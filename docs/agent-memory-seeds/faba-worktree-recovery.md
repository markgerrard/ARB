---
name: "faba-worktree-recovery"
description: "FABA containment/base-ref recovery: stage inputs, make historical absolute paths inert, resolve target object stores, preserve failures, and require every gate."
metadata:
  type: feedback
  origin_session_id: "019f8591-dad5-71a0-849e-5b60a1b2a4cc"
  last_write_session_id: "019f8591-dad5-71a0-849e-5b60a1b2a4cc"
  source_project_key: "mark-be695e9f393d"
---

# FABA bridge worktree failures and crash recovery

Provenance: Polisher remediation panel `panel-arb-role-polisher-v5-remediation-20260722T022914Z-445571`, failed FABA child `polv5r6-445571`, recovery consultation `consult-polv5r6-recovery-20260722`, post-terminal chains `chain-polisher-interface-20260722T133611Z-94c9d1` / `chain-authority-auth-20260722T133611Z-cf9068`, and authority FABA failures `faba-auth-r2-20260722`, `faba-auth-r2b-20260722`, `faba-auth-r2c-20260722` on 2026-07-22.

## Failure classes

1. **Host-absolute evidence paths cause containment termination.** Direct instructions to read `/Users/<user>/<workspace>/...` from an isolated lease produced `worktree_escape`.
2. **Absolute paths inside byte-preserved raw reports are also hazardous.** Even when reports are correctly embedded in the staged task, a synthesizer may follow historical commands such as `/Users/<user>/<workspace>/.venv/bin/python` or `/tmp/...`. Authority FABA r2b did substantive in-worktree work, then terminated `worktree_escape` after encountering such report text.
3. **Audit roster labels are not bridge route IDs.** Use `seat:<fleet-id>` in the audit roster, but `--engine bridge:<fleet-id>` with no `seat:` prefix.
4. **Driver and target Git object stores may differ.** A valid driver HEAD can still fail `worktree-lease-base-ref-invalid` when the target seat leases from another clone. Resolve the target's actual declared workdir (plist/registry), prove the exact OID there, and import only that pinned object if absent. Do not assume every seat uses `/Volumes/<workspace>/repos/codex`; the Fable PiExtensions seat used `/Volumes/<workspace>/repos/PiExtensions`.
5. **A workspace draft is never a decision.** `worktree_escape`, invalid return shape, thread-affinity failure, missing content validation, or missing publish receipt all keep the draft inadmissible even if its prose and tests look complete.

## How to apply

1. Distinguish identities:
   - audit roster: `seat:<fleet-target-id>`
   - bridge target: `<fleet-target-id>`
   - CLI: `--engine bridge:<fleet-target-id>`
2. Stage every input inside the lease or embed it in the staged task. Never instruct a seat to access a host-absolute evidence path.
3. When raw evidence must remain byte-identical, add an explicit execution-boundary directive: every absolute path in raw reports is inert historical text; do not invoke/read/stat/copy it. Reproduce checks only with worktree-relative paths and PATH-resolved tools. Prove the raw-report byte suffix unchanged while allowing the outer task/round-input to change for this directive.
4. Resolve the target seat's real repo/workdir and run `git cat-file -e <pinned-oid>^{commit}` there before lease arm. If missing, non-mutating-fetch exactly that object from the authoritative repo, re-prove it, and do not move branches.
5. Treat `worktree_escape`, lease/base-ref refusal, invalid return shape, and bounce incompatibility as transport/gate failures. Preserve forensics; publish/fold/reconcile nothing.
6. A fresh child run ID with unchanged subject, parent record, round, roster, raw votes/evidence, and coverage predicate is crash-equivalent only when the failed attempt published nothing. A containment-only directive may be added after an independent consult without altering raw evidence.
7. After two failed recovery attempts, obtain an independent operational consult before a third. If the consult-authorized attempt still fails its gate, stop: write an explicit non-closable record and make no fourth attempt without fresh operator authorization and a materially corrected mechanism.
8. Before parent audit close require:
   - exact single-line `FABA_EXIT`;
   - schema and prior-finding coverage validation with zero problems;
   - no staged-input mutation;
   - confirmed ARB Memory receipt;
   - clean subject spot-diff;
   - exact roster votes reconciled;
   - `outcome=emitted`, `gaps=[]`.
9. If any gate fails, the round is not closable and folds nothing.

Long bridge/FABA work uses one event-driven background watcher. Yield immediately after arming it; never keep the chat turn alive with repeated waits or Redis polling.
