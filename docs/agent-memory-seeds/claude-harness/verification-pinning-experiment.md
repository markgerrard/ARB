---
name: verification-pinning-experiment
description: "Acceptance rules pinned into ARB CLAUDE.md on 2026-07-26; a before/after experiment is running, so don't unpin or rewrite that block casually"
metadata: 
  node_type: memory
  type: project
  originSessionId: 4e5b090c-eda4-41cc-ad07-1940ba371aa4
  modified: 2026-07-26T11:12:27.915Z
---

On 2026-07-26 a block titled "Acceptance rules — pinned into always-loaded context" was appended to
ARB's `CLAUDE.md` (commit `80177b5a`). It is deliberate duplication of rules that also live in
`AGENTS.md` and in superpowers skills, and it is **exempt from the repo's no-generic-content rule** —
the whole point is that referenced rules do not arrive.

The date in that block's marker comment is an **experiment boundary**, not decoration. A before/after
comparison is running against it, so treat the block as instrumented: don't casually reword, move, or
"deduplicate" it, and if it must change, note the change against the boundary date.

**Why it was pinned:** a transcript sweep found ARB loaded a verification skill in **0 of 48**
dispatch-containing sessions (fleet-wide: 13 of 193, ~7%). The rules were available and never
arrived. Full measurement, method limits, and the **pre-registered readings** are in ARB Memory
`art-36b75b74831f0aca`.

**Read the pre-registration before interpreting any result** — the readings were committed in advance
specifically so they can't be chosen after the fact, and there is a recorded confounder: this seat
gained structural machinery at the same time, so a post-pin improvement is not cleanly attributable
to pinning.

**Propagated 2026-07-26:** `dev` pushed to origin (`7a9e6235..80177b5a`) and `<workspace>` fast-
forwarded, so both clones hold the identical `CLAUDE.md` blob. A machine-wide `~/.claude/CLAUDE.md`
(42 lines, universal solo-work rules only) was created the same day.

**There are THREE checkouts of the same repo on this host**, not two: `/Volumes/<workspace>/repos/ARB`,
`/Users/<user>/<workspace>`, and `/Users/<user>/AgentRedisBridge` — all `markgerrard/ARB`.
`CLAUDE.md` is therefore one tracked file with one blob. Propagate by sync, never by editing a
clone's copy, or you fork one file into divergent versions. Transcript-directory names reflect
working directories, not repositories, so a seat list will not enumerate them — use
`git remote get-url origin`. All three were at blob `6adf36aa` and carrying the pin as of
2026-07-26 (AgentRedisBridge was 22 behind and fast-forwarded that day).

**Artefact head is v4. Do NOT read v3 — it is a bad write.** A session fetched v1, assumed it was
head, and republished, silently reverting v2's edits and deleting an incident write-up. v4 restores
v2 byte-for-byte plus the third-checkout finding. **Before republishing any stored artefact, probe
upward with `memory_get` until it returns null** — `memory_recent`'s window does not reveal the head
version, and the store auto-increments, so a wrong base is only detectable from the returned version
number. Reconstruct-and-hash-verify against `content_hash` before splicing; that is what made this
recoverable. Note `content_hash` is domain-separated (`arb_memory.hash.artefact_hash`), **not** a raw
sha256 of the content — a plain `sha256` comparison will report a false mismatch.

**Still unknown:** the remote bridge-dev seat hosts, which this session could not inspect.

Related: [[checkpoint-context-protocol]], [[round-panel-roster]].
