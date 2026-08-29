# Bus trust model

**Status: current. Co-signed 2026-08-01 (Mark; decision sweep, ARB-B23(d)/B6-F1 ruling).**

## The boundary

**Bus write-access is the trust boundary.** Any identity that can issue writes against the
bridge bus (LPUSH to inboxes, key writes in the `agent_scratch:` namespace) is trusted at the
level of the whole comms plane. The bridge's sender-policy layer is *routing policy on top of
that boundary* — it decides which senders a seat will work for; it is not, and does not claim
to be, an authentication mechanism. Envelope `from` fields are self-declared and unverified.

## The accepted reflector (B6-F1)

Two pre-existing reply paths (`bridge.py` sender-rejected and the turn-timeout refusal) reply
to the envelope's `from` verbatim. A bus writer can therefore forge `from` naming a victim
seat and make other seats push correlated error envelopes into the victim's inbox
(demonstrated by execution — see ARB-B23(d)). Ruling: **accepted and documented, not
aligned**, because under the current single-trust-tier bus a writer can already LPUSH into
any inbox directly — reflection (~1:1, no amplification) adds no capability an attacker
lacks, while silencing rejected senders would reintroduce silence-then-timeout for honest
misconfigured dispatchers (the diagnosis trap ARB-B6 removed).

## Revisit trigger (load-bearing)

This ruling is **premised on the single trust tier**. It MUST be revisited before any change
that creates partially-trusted bus writers — per-identity key ACLs, tenant-scoped write
permissions, or any topology where an identity can write some keys but not a victim's inbox.
Under such a topology seat-reflection becomes privilege escalation and the align option
(rejected senders log-only on both paths) becomes the correct posture. The scheduled
bus-hardening slices (1e–1g, ARB-B17 ruling 2026-08-01) do not themselves introduce
partial-trust writers, but any future slice that does inherits this trigger.

## Sender authentication

Tracked separately as **ARB-B24** (filed with the same ruling): actual sender authentication
(signed envelopes or equivalent) is the structural fix that would make `from` trustworthy and
retire this document's accepted-risk section. Until then, no design may cite sender-policy as
an authentication boundary — that is the overclaim the B6 design prose was explicitly
downgraded to avoid.
