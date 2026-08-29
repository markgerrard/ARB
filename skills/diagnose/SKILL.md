---
name: diagnose
description: Read-only tri-seat root-cause diagnosis with a neutral contamination boundary, driven by a failing test. Use when a failure needs its cause established by an independent blind panel rather than by the author, when authoring or forwarding sealed opaque panel briefs verbatim, or when evaluating a run_record against the frozen skills._diagnose_common validator. Applies no code changes and accepts no steer field - reach for diagnose-steer if you need to declare one.
---

# diagnose

Read-only tri-seat root-cause diagnosis with a neutral contamination boundary.

The skill derives its observable scope from the failing-test trigger, extracts
real repository artifacts, assigns blind candidates deterministically from those
observations, and evaluates the resulting `run_record` with the frozen
`skills._diagnose_common` validator. It does not accept a steer field and does
not apply code changes.

The skill authors and seals opaque panel briefs; the orchestrator forwards
those envelopes verbatim. The orchestrator must not rewrite or summarize the
sealed brief before forwarding.

## Dispatcher contract

Voting seats are bridge-routed with `scripts/agent-dispatch --workspace dev`
and sender `claude-bridge-dev`; the sealed brief JSON is the positional
`<task>` argument. Route `blind` to `--engine codex --target-id
codex-bridge-dev-example --role reviewer`, `alternative` to `--engine agy-print
--target-id agy-bridge-dev --role reviewer`, and `open` to `--engine pi-sdk
--target-id pi-sdk-bridge-dev-minimax-m3 --role judgment-oracle`.
The `scribe` role is not a voting seat; run it as an in-process cold Agent-tool
subagent with model `claude-haiku`, return its submission with `from="scribe"`
(the gate's `_panel_blocks` expects the scribe submission's `seat == "scribe"` —
a non-`"scribe"` value fail-loud-blocks `unverified-without-panel`), and keep its
reply out of certifier and collation post-briefs. The read-only ceiling is
enforced on M3 and attested on codex/agy; the neutral gate treats that as an
honest operational limit, not a mechanically proven filesystem sandbox.

Semantic exclusivity remains an attested residual: the mechanical gate verifies
the pre-registered predicate shape, independent certification, reciprocal-cycle
absence, evidence-category consistency, scope integrity, scribe isolation, and
channel separation. Whether a certified predicate is semantically strong enough
is panel-judged rather than mechanically over-claimed.
