---
name: bridge-protocol
description: Run a Bridge change through the declared build pipeline (design -> panel -> spec -> panel -> plan -> panel -> TDD build -> tri-review -> merge-gate) and evaluate its executable merge gate. Use when a change to AgentRedisBridge must be gated before merge, when producing or validating phase_input.json / gate_result.json records, when choosing a phase correctness_basis (manual-panel, diagnose, diagnose-steer, external-base-case, hard-signal), or when questions arise about gate/gate.py, gate/trust_root.json, stale phase records, or why the gate blocked a merge.
---

# bridge-protocol

Use this skill when a Bridge change must run through the declared build pipeline:

`design -> panel -> spec -> panel -> plan -> panel -> TDD build -> tri-review -> merge-gate`

The skill owns the executable merge gate in `gate/gate.py` and the JSON contract artifacts in
`gate/`. Gate inputs are builder-supplied `phase_input.json` records; gate decisions are produced only as
`gate_result.json`. Builder-supplied `gate_decision`, `block_reasons`, `verified`, or `judged` fields in
phase input are invalid.

## Correctness Bases

Every phase has a `correctness_basis`:

- `manual-panel`
- `diagnose`
- `diagnose-steer`
- `external-base-case`
- `hard-signal`

`manual-panel` is the bootstrap fallback. `diagnose` may validate blind stages only after it is
merged and verified. `diagnose-steer` may validate steered stages only after it is merged and verified.
Until then, those modes use the manual base case. A phase record with a weaker basis is stale when a
stronger basis is available at merge time. A phase record resting on a validator later marked
`invalidated` is stale in the downward direction too.

## Bridge-Protocol Root

**The certified rules live in `gate/root_rules.md`.** They moved there on 2026-08-08 so that
editing this guide no longer invalidates the trust root — `7ee22909` had done exactly that with
five lines of frontmatter, and it also meant corrections to these docs required a rotation.

Read `gate/root_rules.md` for what the certified object covers, who may rotate the root, and —
importantly — **what the gate does and does not establish**. Short version: it detects drift in
the certified object. It does not authorise anything, and cannot, because every input it reads
is writable by whoever is making the change. Authorisation lives in the panel audit trail and
in a human.

## Load-Bearing Manifest

Code designs/specs/plans provide `load_bearing_components.json`. The gate checks registry-required
dimensions against `costly_dimensions`, dimension-preserving tests, and approved waiver records. The gate
enforces presence and record resolution; waiver correctness remains panel-judged and visible.

## Running The Gate

Import `gate.py` or invoke it from a thin wrapper with a phase input and repo path. The evaluator is
fail-closed:

- open P0/P1 findings block;
- missing or stale executable evidence blocks;
- missing load-bearing dimensions block;
- unclassified production changes block;
- declarative `verified:true` blocks;
- circular or stale validator bases block;
- self-certification blocks;
- builder-supplied gate output blocks;
- open escaped-defect obligations block;
- stale trust root blocks.

The dogfood suite in `tests/test_bridge_protocol_gate.py` covers those controls with exact block reasons.
