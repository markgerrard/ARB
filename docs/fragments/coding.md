# CODING.md — generic coding layer

> Assumes the universal principles in `AGENTS.md`; read that first. This file adds rules for
> writing and changing code, and applies to *any* code session regardless of language or
> framework. Language-, framework-, and repo-specific rules live in the per-repo file, which
> back-references both this layer and the base.
>
> **Tradeoff:** like the base, this biases toward caution over speed. For trivial changes, use
> judgment — the cost of a rule should not exceed the cost of the mistake it prevents.
>
> **Inclusion test for anything added below:** would this be true and useful for *any* code
> session, in any language or framework? If it names a language, a framework, a build tool, or
> a convention specific to one codebase, it belongs in the repo file, not here. A coding rule
> that assumes one stack trains sessions on other stacks to skim the section — the same
> degradation the base warns about, one layer down. If this layer ever thins to a rule or two,
> or its rules turn out stack-specific, collapse it into the base or the repo file rather than
> keeping a file that doesn't earn one.

What the base already carries, so this layer does *not* restate: stating assumptions and
surfacing ambiguity (Unknowns, Claims vs. evidence), reading the target before editing
(Source before analysis, Verification), and not over-building (Simplicity). The code-specific
instances of those follow; the principles themselves are upstream.

---

## Surgical changes

**Touch only what you must; clean up only your own mess.** This is Scope (base) in code: every
changed line should trace directly to the request. When editing existing code, don't "improve"
adjacent code, comments, or formatting, and don't refactor what isn't broken. Match the
existing style even where you'd do it differently — consistency with the file beats your
preference. If you notice unrelated dead code, mention it; don't delete it.

On orphans: remove imports, variables, and functions that *your* change made unused — that's
your mess. Leave pre-existing dead code alone unless asked to remove it — that's a separate
task with its own review.

---

## Simplicity in code

**Simplicity (base), applied to code.** The least code that solves the problem; nothing
speculative. No abstraction for single-use code, no configurability that wasn't requested, no
error handling for impossible scenarios. Two quick checks: if you wrote 200 lines and it could
be 50, rewrite it; and if a senior engineer would call it overcomplicated, it is.

---

## Goal-driven execution

**Define success criteria up front, then loop until they verify.** Turn vague tasks into
verifiable goals before writing code:

- "Add validation" → write tests for the invalid inputs, then make them pass.
- "Fix the bug" → write a test that reproduces it, then make it pass.
- "Refactor X" → confirm tests pass before and after; behaviour is the invariant.

For multi-step work, state a brief plan with a check per step:

```
1. [step] → verify: [check]
2. [step] → verify: [check]
3. [step] → verify: [check]
```

Strong criteria let you loop independently; weak criteria ("make it work") force constant
clarification. Two guards on the loop, carried up from the base because the loop is where they
bite:

- **Don't weaken the evidence to make the conclusion look true** (base, Tests and checks). The
  loop's job is to make the code satisfy the check, never to make the check easier to satisfy.
  A failing test is information. Change it only when you can say why its expectation was wrong —
  not to turn the loop green.
- **Update the plan when evidence contradicts it** (base, Consistency). If a step's check
  reveals the plan was wrong, revise the plan rather than forcing the step through. Your earlier
  plan is evidence, not authority.

---

<!-- Add further coding-generic rules below this line.
     Apply this layer's inclusion test to each one before it goes in. -->

---

**This layer is working if:** diffs contain fewer unrelated changes, fewer rewrites are needed
for overcomplication, clarifying questions arrive before implementation rather than after a
wrong turn, and tests pass because the code is right — not because the checks were loosened.
