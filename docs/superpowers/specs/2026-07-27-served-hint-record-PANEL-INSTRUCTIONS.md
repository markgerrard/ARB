# Served-hint record — standing panel instructions

**For whoever authors the review brief for any round of
`2026-07-27-served-hint-record-design.md`.** Standing, not round-specific. The companion to
`…-FOLD-INSTRUCTIONS.md`: that file governs the author, this one governs the panel.

## The settled-scope guard MUST be embedded in the brief, not linked

From v6 onward the spec no longer carries its own `CLOSED` and `Settled scope` sections — they live
in `2026-07-27-served-hint-record-PANEL-RECORD.md` behind a one-line pointer.

**Every brief MUST therefore:**

1. **Copy the `Settled scope — do NOT re-litigate` and `CLOSED` blocks from
   `…-PANEL-RECORD.md` verbatim into the brief itself**, under a heading that says they are out of
   scope. Do not replace them with a link.
2. **Instruct reviewers to read `…-PANEL-RECORD.md` before the spec**, and state that items marked
   settled there carry the same authority they had when they were inside the spec.
3. **Give reviewers the challenge escape:** if a reviewer believes a settled item is genuinely
   wrong, say so once, in one sentence, marked `OUT-OF-SCOPE-CHALLENGE` — not spend the round on it.

**Why embedding, not linking.** A reviewer's whole disposition is to find things to argue with, and
panels skim. A pointer sitting inside a ~90KB document is a weak instrument for redirecting that
disposition — it competes with everything else in the file and is read, if at all, after the
reviewer has already formed opinions. The settled block is ~2KB; the brief already embeds the entire
spec, so the marginal cost is nil and the guard lands where reviewers actually read.

**What breaks if this is skipped.** The brief author reads the spec, finds no settled-scope section
(correctly — it moved), and omits the guard. The round then re-argues the §4 single-writer question,
the co-signed §3 MUST, or the owner-approved structural directive, all of which were closed rounds
ago. The relocation would have quietly destroyed a guard rail it was supposed to preserve.

## DO NOT EMBED THE SPEC — reference it, pinned

**Binding from round 6.** The brief must NOT contain the spec verbatim. Reference it:

- the path in the reviewer's own worktree,
- the sha256 of the exact bytes,
- the ARB Memory artefact id and version,
- an instruction to verify the sha before reviewing, and to stop and report a mismatch rather
  than review the wrong bytes.

**Why.** Earlier rounds embedded the spec, on the reasoning that a self-contained brief removes
ambiguity about which version is under review. That reasoning was wrong: each seat's worktree is
created at an explicit `--worktree-base` commit, so **the version is already pinned by
construction** — embedding added payload without adding certainty.

The cost became fatal at v6. The brief reached 139KB against the bridge's 128KB
(`--max-message-bytes` default 131072) envelope limit, and **all four dispatches were refused
`message-too-large`** with no reply. Replacing the embedded spec with a pinned pointer took the
brief from 138KB to 36KB.

This is the same growth pressure that drove the size budget and the partition plan, arriving in a
third place: anything that scales with total document size eventually hits a ceiling. A pointer
does not scale with the document.

**Still embedded, deliberately:** the `CLOSED` and `Settled scope` blocks (~2KB). Those are guard
rails that must be unmissable, and they do not grow with the spec.

## Standing brief content, unchanged

- **Foreground the fold.** A fold is authored by one seat, in one pass, against a findings list, and
  is unreviewed until the panel sees it — structurally the least-reviewed surface in the document.
  The "What changed" table is the author's *claim*, not evidence; reviewers verify each row against
  source. Round 2's highest-value finding was a fix crediting the wrong mechanism; round 3's were
  three defects the fold itself introduced.
- **§9 must be able to fail.** Round 4's headline finding (H-03) was that tests named as tripwires
  for specific findings could not fail on those findings. Every subsequent panel checks this
  explicitly: for each test §9 names, ask whether it would fail if the defect it labels were
  present.
- **Design review, no diff.** No implementation exists for this slice; absence of code is not a
  finding.
- **Verification honesty.** The verify commands need `ARB_MEMORY_DSN`. They skipped 4/4 in every
  seat in rounds 1–4. A skip is not a pass — report skips as skips.
- **Canonical vote fence.** Stance is exactly one of
  `abstain | approve | block | needs-changes | timed-out`, with `severity` one of
  `none | P2 | P1 | P0`. Any other stance string is rejected by the parser and costs the round a
  vote re-fire. The fence goes in the **inline reply**, not only in the report file.
- **Independence.** Each bridge seat gets its own worktree; reviewers do not read each other's
  reports, this round's or prior rounds'. Note prior rounds' reports are present in the checkout —
  their presence is not permission to read them.

## Size regression check — the panel's, not the author's

From v6 the fold runs under a binding line budget (`…-FOLD-INSTRUCTIONS.md`). **The brief author
should record, and the orchestrator should check, the per-section byte table — not just the total.**

A budget met by thinning §9 or flattening the contested-findings records, while §4 holds its share,
is the bill being paid from the wrong account. §4 was 43% of v5 (41KB of 97KB); the compression rule
targets that share falling toward ~30% via de-duplication. Total-lines-down with §4-share-flat means
the constraint was satisfied, not achieved.
