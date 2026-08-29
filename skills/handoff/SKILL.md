---
name: handoff
description: Write a handoff document so a fresh agent can continue this work after context is cleared — or pick one up and resume. Use when the user wants to clear context mid-task, end a session with work in flight, hand the current thread to another session, or resume from an existing handoff. Triggers on "handoff", "hand off", "write me a handoff", "about to clear context", "pick this up in a new session", "pick up from handoff", "catch up from handoff", or the user pasting a `.claude/handoffs/` path.
argument-hint: What will the next session be used for?
---

# Handoff

Two modes — pick by what the user asked for:

- **Write** (default): produce a handoff document so a fresh agent with zero
  context can continue this work.
- **Pick up**: the user pasted a handoff path or said "pick up / catch up from
  handoff" — read it and resume. If no path was given, use the newest file in
  `.claude/handoffs/`. First verify the header against current repo state
  (HEAD moved? tree dirty? volatile state still true?) and flag any drift
  before trusting claims. Then read the "Read first" files in order, confirm
  your understanding of the focus and next step in 2–3 lines, and start on
  the ONE unconditional next step — don't re-ask what's already answered in
  the document.

## Where to save

Save to the current project directory as `.claude/handoffs/<YYYY-MM-DD-HHMM>.md`
(create the directory if needed). This is working state, not project history — do
not commit it unless the user asks; if `.claude/handoffs/` is not already
gitignored, say so.

**Print the full file path as the final line of your reply**, on its own, so the
user can paste it straight into the cleared session.

## Required header

```
Generated: <timestamp> | HEAD: <short-sha> | Branch: <branch> | Tree: clean|dirty
```

(Omit the git fields if the project is not a repo — but say so.)

Open the document by instructing the successor to verify this state against the
repo before trusting any claim in it. A handoff read against a moved HEAD is
worse than no handoff — state reflects when it was written, not now.

## What to capture

Only what lives in this conversation and nowhere else:

- **Decisions and reversals** the user made, with their reasoning — especially
  verbal ones not recorded in any artifact
- **Pending approvals and explicit do-NOTs** — things awaiting the user's go,
  and things the user said not to do
- **Open questions** awaiting the user
- **Dead ends** — approaches already tried and rejected, and why, so the
  successor doesn't re-walk them
- **Exactly ONE unconditional next step** — something the successor can start
  immediately without waiting on anyone — then the queue behind it

Do NOT duplicate content already captured in other artifacts (PRDs, plans,
ADRs, issues, commits, diffs, docs). Reference them by path or URL instead.
The handoff's value is precisely the residue that exists only in conversation.

## Orientation for the successor

- Lead with a **"Read first"** list: at most 3 files, in order, with one line
  each on why
- Then a **"Suggested skills"** section: skills the successor should invoke,
  with when/why
- If any referenced state is volatile (running processes, queues, remote
  sessions, background tasks), say how to check it's still true

## Hygiene

- Redact secrets, API keys, passwords, tokens, and PII — reference where they
  live (e.g. ".env", a vault) rather than their values
- Write for a cold reader: no unexplained nicknames, abbreviations, or
  "as discussed above"

## Focus

If the user passed arguments, treat them as the next session's focus.

If they did not, **ask before writing** — derive the plausible focus candidates
from the conversation's open threads and offer them as options (use the
AskUserQuestion tool if available, plain question otherwise):

- 2–4 concrete options, each a distinct open thread, most likely continuation
  first
- plus an "everything — general continuation" option for when the user just
  wants context preserved without narrowing
- give each option a short markdown `preview` (the would-be next-step queue
  for that thread) — previews switch the UI to the side-by-side layout where
  the user can attach a note to their selection (single-select only)
- if the user attaches a note to their selection (or answers via "Other"),
  fold it into the focus — notes refine or override the option label

Then weight the document toward the chosen focus and aggressively trim
everything orthogonal to it — a focused successor doesn't need the whole
story, just its thread. For "everything", keep all open threads but still
rank them: most-likely-next first.
