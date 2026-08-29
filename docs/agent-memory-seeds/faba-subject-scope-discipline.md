---
name: "faba-subject-scope-discipline"
description: "FABA subject scope must stay on the explicitly designated artefact; incidental edits or discussion do not authorize switching subjects."
metadata:
  type: feedback
  origin_session_id: "019f8591-dad5-71a0-849e-5b60a1b2a4cc"
  last_write_session_id: "019f8591-dad5-71a0-849e-5b60a1b2a4cc"
  source_project_key: "mark-be695e9f393d"
---

# FABA subject-scope discipline

Standing correction from Mark (2026-07-22).

## Why

Codex misread “Fable tweaked the GitHub enhancement issue” as meaning the issue should become the next FABA subject. It launched and published a schema-valid review of the issue even though the standing FABA rounds were explicitly about the Polisher. A technically valid gate does not cure a wrong subject.

## How to apply

- Bind every FABA launch to the explicitly designated subject and workflow lineage.
- Incidental edits, side discussions, GitHub issues, implementation commits, or other artefacts do not become FABA subjects unless Mark explicitly changes the subject.
- Before launch, state the exact subject ID, current version, prior decision-record ID, and why this is the next round in that lineage.
- If the referenced change is to a different artefact, treat it as context only and keep the current FABA subject unchanged.
- When the designated subject is the Polisher, run FABA only on the Polisher artefact or a specifically authorized Polisher revision/closure audit.
- A FABA record produced against the wrong subject is off-piste: mark it non-authoritative, do not audit-close it, and fold nothing.
- The latest valid Polisher closure as of this correction is run `panel-arb-role-polisher-v5-remediation-20260722T022914Z-445571`, `outcome=emitted`, `gaps=[]`, verdict `block/P0`. A next content round requires a new Polisher revision; a closure-only audit requires explicit authorization.
