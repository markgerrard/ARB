# Trusted agent-sdk same-cwd continuation

## Problem and invariant

`AgentSdkEngine.resume_thread()` looks up its transcript using
`project_key_for_directory(cwd)`. The existing trusted, stateful agent-sdk
admission rule correctly requires a fresh isolated worktree, but a fresh
worktree has a different project key and cannot be revisited by the ordinary
`git worktree add` path. The design must make a same-cwd resume reachable
without reopening trusted writes against the bridge's shared base checkout.

The chosen invariant is: a trusted stateful continuation runs only in the
bridge-created, still-registered worktree that created its session, and only
for the same trusted sender. It never accepts a caller-selected existing path.

## Mechanisms considered

### 1. Continuation-only exemption to run in the base checkout

Allow a trusted request with `thread_id` to omit `payload.worktree` and run on
the pooled engine rooted at `AGENT_WORKDIR`. This directly restores the SDK
project key for sessions originally created in the base checkout, but relaxes
the primary rail: trusted code can write the shared base checkout. It also
allows concurrent reviewers to see each other's base-tree changes and lets any
trusted sender that learns a session ID attempt to read or continue that
session. Reject: the successful path depends on undoing the protection the
admission rule was introduced to enforce.

### 2. Persistent named worktree, recorded per session (chosen)

The first trusted stateful request still supplies a normal new worktree with
`cleanup: keep`. Once a successful turn returns a session ID, the bridge writes
an append-only local record binding that ID to the creating sender and the
worktree name. A later trusted request supplies only `thread_id`; the bridge
looks up the record, verifies the sender, validates that the path remains
under `.claude/worktrees`, confirms it is still registered by `git worktree
list --porcelain`, and launches a fresh agent-sdk engine with that worktree as
its cwd. This preserves base-checkout write isolation and prevents ordinary
concurrent reviewer read-leak because each conversation remains in its own
worktree; the session-routing index is hashed by session ID, mode 0600,
append-only, and owner-bound, protecting session-store integrity from a
different trusted sender or a path substitution.

### 3. Copy or re-key the session transcript into every fresh worktree

Create a new worktree per continuation, then copy the SDK JSONL transcript to
the fresh cwd's project key. This retains base write isolation but treats the
session store as a portable artifact without a transaction across the source
and destination. Concurrent continuations can fork or corrupt transcript
history, stale copies can silently lose turns, and copying a transcript into a
caller-selected worktree makes a session disclosure primitive. Reject: its
integrity rules are substantially harder than retaining a single authoritative
cwd, while it solves no required concurrency case.

### 4. Stable synthetic project key shared by all worktrees

Change the session-store key from directory-derived to a repository or seat
identifier so new worktrees find old transcripts. This preserves write
isolation, but joins every concurrent worktree for that repository into one
session namespace. It weakens reviewer isolation and creates session-ID
collision/ownership ambiguity unless another mapping layer is introduced;
that layer converges on mechanism 2 but with broader blast radius. Reject.

## Chosen protocol and refusal behavior

1. Start a trusted stateful agent-sdk task in a new `payload.worktree` with
   `cleanup: keep`.
2. On a successful result with a session ID, persist `{thread_id, sender,
   worktree_name}` under the agent-sdk session root. Continuations also take a
   non-blocking, cross-process `flock` lease keyed by session ID, so two callers
   cannot concurrently resume and append to the same transcript/worktree.
3. Continue by sending `{task, thread_id}` with no `worktree`. The bridge
   resolves the stored worktree and calls `resume_thread()` on an engine whose
   cwd is that same directory.
4. Refuse a missing/deleted/unregistered mapping, another trusted sender, a
   caller-supplied worktree on a continuation, malformed/corrupt mapping data,
   and all ordinary non-continuation trusted stateful requests without a new
   worktree.

The capability deliberately added is narrow but real: the original trusted
sender can cause a later agent-sdk engine to read and mutate the *persistent
worktree* associated with a session ID, rather than only a fresh one-shot
worktree. A misbehaving engine can therefore leave state in that persistent
worktree for a later continuation, and it can continue to mutate it across
dispatches; it still cannot reach the shared base checkout through bridge
routing, and a different trusted sender cannot use the recorded session ID.
The remaining operational risk is worktree lifecycle: kept continuation
worktrees accumulate until an operator explicitly removes them, at which point
continuation fails safely rather than falling back to the base checkout.
