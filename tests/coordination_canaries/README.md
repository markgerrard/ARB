# Coordination-plane cutover canaries

Built 2026-08-12 to answer P1-5 of `codex-arbmem-prod`'s adversarial review of the
cohort-affinity amendment (ARB Files
`handoffs/cohort-affinity-amendment-adversarial-review-codex-prod-20260812.md`,
sha256 `9c60b6e2…`). That finding says the amendment's evidence base is
**category-mismatched**: the Mini precedent and the n=4 enumeration are AUDIT-plane
evidence, and neither exercises a DB12 **coordination** cutover. These canaries are
the missing evidence.

They do not decide anything. They characterise the boundary so that the amendment's
§4 closure table can be rewritten from claims into measurements.

## Running them

```bash
.venv/bin/python -m pytest tests/coordination_canaries/ -q      # ~12s
```

They need `docker` and a locally cached `redis:7-alpine`, and **skip loudly** with the
reason when either is missing. They never pull over the network.

## Why real servers instead of the fake-redis shim

Every property under test is one a shim would define away. "Two watchers on two buses
have no shared atomic claim" cannot be observed against a fake whose atomicity is
whatever its author implemented, and "the same envelope id on both buses" needs two
genuinely independent servers. So each canary runs the **real scripts** against two
real instances.

## Safety

A coordination canary is a failure-injection harness: it strands envelopes, SIGKILLs
consumers mid-flight, and pushes forged senders. Pointed at a shared bus it would be
indistinguishable from an attack. `conftest._assert_local` refuses any non-loopback
endpoint and is re-checked **on every plane handout**, not once at construction —
`test_canary_harness.py` proves a mutated host is still refused.

## Coverage against prod's required-evidence list

| prod required | canary | result |
|---|---|---|
| idle dispatch | `test_c1_idle_flip_delivers_cleanly_and_strands_nothing` | clean (this is the CONTROL) |
| active dispatch | `test_c2_request_in_flight_at_flip_time_…` | request + reply path stranded on the old plane |
| late foreign send after depth-zero | `test_c3_late_foreign_send_…` | stranded; depth-zero is not a barrier |
| reply arrival at the boundary | `test_c4_reply_to_the_old_plane_…` | unreachable; indistinguishable from "peer still working" |
| crash with an item in `:processing` | `test_c5_*` (4 canaries) | no loss; but recovery re-wakes the agent |
| same envelope id on both buses | `test_c6_*` | **two wakes, one file** |
| shared-sender / shared-seat refusal | `test_c7_*` | a foreign credential presents as a trusted sender |
| stale target backlog | `test_c8_stale_target_backlog_…` | surfaces ahead of live traffic, no discriminator on the wire |
| rollback | `test_c9_rollback_…` | residue stranded; **presence is live on BOTH planes** |

Two findings are not from prod's list — they surfaced while building:

- **`test_canary_ordering.py`**: the reliable watcher (`BLMOVE … RIGHT`) drains a
  backlog **oldest-first**, while the operationally-armed BLPOP split watcher drains it
  **newest-first**. §5.2's archive+diff+drain is ordering-sensitive, so which consumer
  is running changes what an operator sees first. Found because an ordering assertion
  failed and the instrument turned out to be at fault (the harness was RPUSH-ing where
  real senders LPUSH).
- **`test_c9_…`**: after flip-and-rollback both planes report the identity alive, so
  presence surfaces cannot answer "which plane is this identity consuming?".

## Assertion direction — read before "fixing" a red

Most canaries assert that **loss or duplication HAPPENS**. That is not an endorsement;
it pins the boundary so it cannot change silently. If an admission fence, forwarder,
tombstone or cross-plane claim later lands, the corresponding canary **should go red**,
and that red is the signal a §4 row may finally be rewritten to "closed". Each such
assertion carries a message saying exactly that. A canary asserting the
desired-but-absent behaviour would just be a failing test nobody can act on.

## Mutation check (2026-08-12)

Applying wake-level dedup to both emit paths of `agent-inbox-watcher-reliable` —
i.e. the fix P1-4 asks for — turned exactly **two** canaries red
(`test_c5c_…`, `test_c6_…`) and left the other 20 green. The suite measures those
defects and not their neighbours. The mutation was reverted and the script verified
byte-identical afterwards.

## What these canaries still do NOT cover

Stated explicitly, because a suite that looks comprehensive is worse than one with a
known edge:

- **No Postgres acceptance.** prod requires cutover acceptance to include a persisted
  gapless verdict row with no deadletter. These run against local buses with no
  Postgres, so they prove coordination-plane behaviour only. The verdict half of
  acceptance still has to come from a real audited run.
- **No ACL-shaped cohort refusal.** `test_c7_*` shows sender identity is unbound; it
  does not exercise the graph-derived cohort closure P1-1 asks for, which needs live
  identity/credential topology rather than two throwaway ACL users.
- **No real multi-second network partition** — the planes are independent, not
  partitioned mid-flight.
