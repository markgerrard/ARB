# Bridge-Protocol Root — the certified rules

**This file is inside the certified object** (`gate.logic_set_paths`). Editing it moves
`certified_object_sha` and trips the gate. That is deliberate: these are the rules governing the
root, so they must not be changeable without the change being visible.

The narrative guide is `../SKILL.md`, which is **not** certified — documentation can be corrected
without a rotation. Before 2026-08-08 it was certified, and the result was that fixing a sentence
required a full rotation ceremony.

## What the gate actually establishes

**The gate is a DRIFT DETECTOR. It does not authorise anything.**

It re-hashes the certified object on every run and compares against the sha recorded in
`gate/trust_root.json`. A mismatch means: *the logic set is not the one someone recorded.* That is
genuinely useful and it is the whole of what the gate proves.

It does **not** establish that a rotation was reviewed, that certifying seats exist or agreed, or
that a human approved anything. It cannot, and this is structural rather than a gap to be closed:

> Every input the gate reads — `trust_root.json`, any rotation record, `gate.py` itself — lives in
> the repository the change is being made to, and is writable by whoever is making the change. A
> gate cannot authenticate changes to the tree it lives in.

Concretely, and verified: editing `certified_object_sha` in `gate/trust_root.json` to the running
value makes the gate pass, with no review of any kind. That is not a defect to be patched — it is
the boundary of what a repo-local check can do. A check added to "prevent" it would live in the
same repo and be editable the same way.

Two rounds of an external panel established this the hard way
(`panel-gaterotate-20260808T045453Z-4acada`, `panel-gaterotate-r2-20260808T111742Z-fdbcb3`), the
second after an attempt to wire `rotation_blocks` into `evaluate()` was blocked with two P0s: it
added a second way to pass without closing the first, and accepted unvalidated JSON.

**Where authorisation actually lives:** the panel audit trail, which is append-only and
reconcile-gated in Postgres, *outside* this checkout — and a human. `gate.rotation_blocks` encodes
the contract as a checklist for that process. It is not called by `evaluate()` and must not be
without first solving the external-basis problem above.

## Rules

Bridge-protocol never certifies itself. Its own phase records use `external-base-case`: a
human-judged tri-panel root pinned by `gate/trust_root.json` after external review. Workers must not
author the real trust root for their own build. They may author `gate/trust_root.pending.json` as a
schema-valid placeholder.

Rotating the root requires certifying seats disjoint from the prior root's, a `change_author` who is
not among them, and a human approver. **The gate does not check these** (see above); the panel and
the human do. A seat that cannot execute — no shell, so it cannot recompute the sha or run the
suite — cannot certify, and should say so rather than vote.

The certified object hash covers exactly these paths — all of them, which the pre-2026-08-08 text
got wrong by omitting the `defect_hunts` entries:

- `gate/gate.py`
- `gate/root_rules.md`
- `gate/schemas/gate_result.json`
- `gate/schemas/layer_registry.json`
- `gate/schemas/load_bearing_components.json`
- `gate/schemas/phase_input.json`
- `gate/schemas/stage_modes.json`
- `gate/schemas/trust_root.json`
- `gate/schemas/trust_root_rotation.json`
- `gate/schemas/validator_readiness.json`
- `../defect_hunts/h2_assumptions.py`
- `../defect_hunts/h2_derive.py`
- `../defect_hunts/h2_graduation.py`

Mutable data such as `trust_root.json`, `layer_registry.json`, `validator_readiness.json`,
`stage_modes.json`, `gate/results/*`, and `*_result.json` is not part of the certified object hash.
Changes to certified files require root rotation; data changes are inputs gated by the gate.
