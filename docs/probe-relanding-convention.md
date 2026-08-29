# Probe re-landing convention

**Co-signed 2026-08-01 (Mark; decision sweep) — current.** Slice 1e (probe-package artefact) builds the deep
behavioural-provenance verification this convention references; until it lands, marker-shape and
reinstate-declaration validation (the merged gate) is the enforced surface. A relanded probe is an ordinary pytest module in the
suite tree owning the remediated code. It carries an AST-readable marker:

```python
PROBE_PROVENANCE = {
    "claim_id": "<claim id>",
    "probe_artefact_id": "<store artefact id>",
    "probe_artefact_version": 1,
}
```

Its co-located sidecar must contain at least one `kind: "reinstate"` mutation whose
`expect_failed` names the probe tests. The mutation's fixed bytes are the `find` value and the
defective bytes demonstrated by the probe are the `replace` value; reviewers and Slice 2 verify
that behavioural provenance. The gate validates marker shape and the reinstate declaration,
but does not resolve the store pointer or claim byte-for-byte fidelity: rehydration may adapt
imports and fixtures.
