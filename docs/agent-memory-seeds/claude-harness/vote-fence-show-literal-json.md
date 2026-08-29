---
name: vote-fence-show-literal-json
description: "Panel briefs must show the vote fence as a literal JSON example — grok emitted a YAML-form fence when the brief only described the fields, and the auto-emit dropped its vote"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 5bb925d0-80ee-483f-96bb-e04e8c412b41
  modified: 2026-07-27T19:51:04.067Z
---

Incident 2026-07-27 (Slice 1d-iii r2 panel, run `panel-1diii-impl-r2-20260727T193158Z-1d058a`):
grok-bridge-dev ended its review with a YAML-style fence (`stance: needs-changes` /
`severity: P1`) instead of the canonical JSON object. The bridge's `--audit-panel` auto-emit
dropped the vote silently (fail-soft); detected only because the run's `:seq` counter read 3
where r1 read 5. Recovery: the lead re-encoded the seat's verbatim fence values to the JSON
form and emitted via `panel-run vote` — allowed because the seat had NO landed vote (not a
supersede) and the re-encoding was mechanical, field-for-field, no prose interpretation.

**Why:** briefs in that round said "end with the mandated `vote` fence (stance ∈ …)" —
described the vocabulary but never SHOWED the JSON syntax. r1's identical seat emitted JSON;
the format is not stable under description alone.

**How to apply:** every panel/review brief carries the literal example, verbatim:

    ```vote
    {"stance": "<approve|needs-changes|block|abstain>", "severity": "<none|P2|P1|P0>"}
    ```

And before ANY audit close, compare the run's `:seq` counter against `1 + roster size`
(+1 after verdict) — a short seq means a dropped vote, and the close's reconcile gate is the
backstop, not the detector. Related: [[round-panel-roster]].
