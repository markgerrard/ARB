# ARB Messages — privileged-action broker, Cloudflare `mint` slice (design)

> Status: design, round 5 — **converged, ready for the implementation plan.** See `<!-- r5: -->`
> markers below, in addition to the round-1/2/3/4 markers.
>
> **r5 summary:** three of four round-5 reviewers gave APPROVE-class verdicts with zero P0/P1 —
> pi-GLM independently re-derived, via a cleaner reachability trace than the round-4 report's own
> narration, that the round-4 fix's target scenario is genuinely reachable (through repeated
> lease-reclaims of the same logical request, not — as one narration of the round-4 finding could
> be misread — a `done` row somehow being reclaimed again) and gave a full clean APPROVE; codex
> confirmed the same and found only a stale cross-reference; cold-Opus — the reviewer whose
> round-4 finding this round's fixes address — independently re-ran their own reachability check,
> confirmed the fix is correct and complete, and explicitly recommended no further review round
> was needed. agy-print's REVISE was driven by three findings of a different, lesser class than
> rounds 1–4's "unrecorded live credential" P0s: a genuinely ambiguous "or" in the found-branch's
> completion condition (independently also flagged by cold-Opus, who rated it P2 since the
> governing sentence and regression test both already pin correct behavior — fixed regardless,
> since it sits in the credential-minting hot path and costs nothing to tighten) and two missing
> retry/backoff-tracking mechanisms: `Settings.max_retries` was defined but never wired into
> anything (no schema column, no enforcement — a poison-pill request could crash-loop the
> executor forever), and the deadletter table had no way to distinguish a fresh entry from a
> permanently-failing one, risking an unthrottled retry-storm against Cloudflare's API. Both
> fixed with an `attempts` counter (`arb_messages`, enforced at claim time) and
> `attempts`/`last_attempt_at` (`arb_messages_deadletter`, observability without a hard cutoff).
> Also folded in cold-Opus's three smaller P2s: the found-branch's fail-closed path now
> deadletters its own un-revoked orphans (restoring the same auto-swept backstop the fenced-out-
> worker path already had, rather than leaving them purely operator-required); a stale "both
> sweep categories" reference corrected to "all three" now that the deadletter category exists;
> and `read_and_mark_delivered`'s no-op enumeration extended to `failed`/`denied` explicitly, not
> just `pending`/`claimed`. With these precision fixes, the mint-idempotency mechanism — the
> subject of a genuine finding in four of five review rounds — has converged: no reviewer this
> round found a fresh instance of the "unrecorded, unrevoked live credential" failure class, and
> the two most rigorous reviewers on this specific question (pi-GLM, cold-Opus) each explicitly
> confirmed they could not construct one. Proceeding to the implementation plan.
>
> **r4 summary:** three of four round-4 reviewers gave APPROVE-class verdicts on the core
> mint-idempotency mechanism itself — pi-GLM explicitly walked the full crash/hang/race timeline
> and concluded the pattern of "each round finds one more layer" had terminated, and codex found
> no fourth-layer flaw in the normal crash/hang/remint path. But cold-Opus, tracing one further
> concrete three-worker timeline, found the pattern had NOT fully terminated: round-3's fix
> ("revoke by the ID already in hand, not by a name lookup") was applied only to a fenced-out
> worker's own path, not symmetrically to the reclaiming worker's revoke-then-remint step, which
> still revoked "that orphan token" (singular, found by name) and then overwrote any
> already-recorded `provider_token_id` without ever revoking the ID being overwritten. Because
> Cloudflare permits duplicate names (this spec's own premise) and a failed-to-revoke deadletter
> entry deliberately leaves a token live under that exact name, a later reclaim's name lookup can
> return two live tokens — and revoking the wrong one (or only one) leaves the other unrecorded
> and unrevoked: an unrecorded, unrevoked live orphan, the exact P0 class rounds 1 and 3 both
> exist to prevent. Treated as confirmed at the round-2/3 evidence bar (one concrete, traceable
> failure timeline). Fixed: revocation is now symmetric (a row's own already-recorded
> `provider_token_id`, if set, is always revoked by that specific ID before anything else) and
> exhaustive (a name lookup returning more than one live token — a real case, not hypothetical,
> given CF's own duplicate-name permission — revokes all of them, not "that orphan" singular).
> Also fixed, converging 3/4 (codex, agy-print, cold-Opus): the round-3 deadletter table's drain
> path was promised ("the sweep also processes this table") but never actually mechanized — the
> sweep's real spec only ever enumerated two `arb_messages` categories, neither of which selects
> against the deadletter table's schema at all. Added a genuine third sweep category, a
> `token_revoked_at` completion column on the deadletter table (rows are never deleted, matching
> this design's row-as-audit-record instinct), a "CF 404 on revoke = success" rule applied
> uniformly everywhere this mechanism calls revoke, and `SELECT ... FOR UPDATE SKIP LOCKED` on
> the sweep so concurrent executors' sweep loops don't double-process the same orphan. Separately,
> agy-print found a genuinely distinct functional bug: the shared `read_and_mark_delivered`
> function, as described, would mark `delivered_at` on a poll of a still-`pending`/`claimed` row
> — an agent legitimately checking status early would permanently lose the eventual result once
> the mint actually completed. Fixed by gating the `delivered_at` write on the row being
> genuinely `done` with a sealed body; a poll of a non-terminal row is a true no-op on delivery
> state. Plus smaller P2s: a last-resort log line for the (acknowledged, irreducible) triple-
> failure residual where a fenced-out worker's revoke AND its deadletter INSERT both fail; the
> pause-flag seed pinned to `ON CONFLICT DO NOTHING` so a restart can't silently un-pause an
> active incident; and dropping a caching option for the pause-flag read that would have
> reintroduced exactly the "kill switch has delayed effect" confusion this design otherwise
> avoids.
>
> **r3 summary:** round 2's fixes were verified sound for the crash-timing points they
> explicitly named, but three of four round-3 reviewers (codex, agy-print, cold-Opus)
> independently traced the exact same composition gap the review brief specifically asked about:
> the round-2 fencing guard (`WHERE status='claimed' AND claimed_at=<my_claim>`) fences the
> executor's *database writes*, but not an already-in-flight *external Cloudflare API call* from
> a hung (not crashed) worker. A worker that hangs inside its CF create call past its lease, gets
> its row reclaimed and completed by a second worker, then has its own create call land
> afterward, ends up holding a live, unrecorded, unrevokable orphan token — round-2's "discard
> and log" instruction for a fenced-out write left exactly that token unrevoked and untracked,
> reintroducing the round-1 P0 this whole idempotency mechanism exists to prevent. Round-2's own
> claim that the fence "confirms ownership before the CF mint call" was also simply wrong —
> chronologically impossible, since the token ID isn't known until the CF response returns.
> Fixed: a fenced-out worker whose CF create nonetheless succeeds must immediately revoke the
> token using the ID already in hand, before stopping; if that revoke itself fails, the orphaned
> ID is recorded in a new small deadletter table (not dependent on owning the original row) so
> the sweep or an operator can still find it. Two of four reviewers (pi-GLM, cold-Opus)
> separately converged on a second, related precision gap: invariant 8's "found" branch was
> worded to fire only when `provider_token_id` is NOT yet set, but the crash window between
> invariant 7's write and the seal step means a same-named token can be found with
> `provider_token_id` already set and the secret still irrecoverably lost — the old wording would
> have misdirected an implementor into skipping revoke-then-remint exactly when it's needed.
> Fixed by reframing the condition around body-sealed status, not `provider_token_id` presence.
> Also folded in from agy-print (not independently corroborated this round, but specific and
> well-evidenced): Cloudflare's token-management permission may not actually scope to "tokens the
> holder created" the way invariant 9 assumed — added as an explicit fourth live-verification
> item, with a dedicated-CF-account mitigation noted if confirmed; the revocation sweep must run
> independent of both kill switches, or pausing the plane during an incident also pauses the
> exact cleanup an incident needs; a separator-injection collision in the deterministic
> token-name hash (fixed by hashing components before concatenating, not after); and a redundant
> standalone `agent_id` index (the compound unique index already covers it).
>
> **r2 summary:** round 1's fixes for the door/executor split and `agent_id` identity binding
> were verified coherent by all four round-2 reviewers (codex, cold-Opus, agy-print, pi-GLM) —
> those two P0s are closed. But codex and agy-print independently found, with cited Cloudflare
> API documentation, that round 1's fix for invariant 8 (mint idempotency) has a fatal flaw:
> Cloudflare's token-creation API returns the plaintext token secret **exactly once**, in the
> create response — a subsequent lookup-by-name can find that a token exists (its ID, name,
> expiry) but can **never** recover its secret value. So "adopt the existing token's ID and
> proceed directly to seal-and-complete" is impossible whenever the crash happens after a
> successful mint but before the secret was sealed/persisted — there is nothing left to seal.
> (Two other reviewers, cold-Opus and pi-GLM, read the mechanism as closing the gap; their
> reasoning didn't address this specific fact, and codex's finding cites Cloudflare's own API
> docs directly, so it's treated as confirmed rather than adjudicated by vote.) Fixed below by
> replacing "adopt and complete" with "revoke the orphan, then mint fresh" (or fail-closed if
> revocation itself fails). Also folded in from round 2: `messages_poll` needed an explicit
> actor-ownership check (3 of 4 reviewers converged on this — a caller could otherwise poll
> another agent's request and consume its one-time delivery slot, a DoS); the request-ID/token-
> name dedup key needed per-agent namespacing, not a bare global value; the cited
> `_default_actor()` precedent has an `or "mcp"` fallback that must NOT be inherited on a
> credential-minting plane (2 reviewers); the minting token's live-verification gate was missing
> a list/read-permission check the lookup-before-create mechanism actually depends on (2
> reviewers); the token-revocation sweep didn't cover delivered-but-never-actually-received
> orphans (3 reviewers); a hung-then-recovered worker (distinct from a crashed one) had no
> fencing guard against double-writing a claimed row's result; and several smaller precision
> fixes (token-name length bounding, door kill-switch registration-time-only wording, a config
> required-vs-default contradiction, the `CHECK` constraint's actual guarantee). See inline
> `<!-- r2: ... -->` markers.
>
> **r1 summary:** round 0 was reviewed by a 4-seat panel (codex, cold-Opus, agy-print, pi-GLM),
> all four independently REVISE, converging unanimously on three real security gaps: (1) the
> Containment design imported `EmailTools`'s synchronous check-sequence pattern into a plane
> that is actually a two-process async queue (door enqueues with OAuth context; a separate
> executor claims/mints with **no** MCP auth context at all) — the checks were never assigned to
> the site that can actually run them, and the executor was never named as required to
> independently re-validate a queue row as untrusted input; (2) `arb_agent_keys`
> self-registration had no identity binding, so a caller could register a key for (and hijack
> mints intended for) any allowlisted `agent_id`; (3) "idempotent execution" (invariant 8) was
> only enforced at the Postgres-row level (`UNIQUE` on enqueue), not at the Cloudflare-API level
> — a crash between a successful mint and the DB write recording it would leak a live,
> unrevokable orphan token on lease-reclaim retry. All three are fixed below by restructuring
> § Containment around an explicit door-side/executor-side split, binding `agent_id` to the
> authenticated OAuth actor everywhere, and adding mint-time idempotency
> (deterministic-name lookup-before-create + immediate `provider_token_id` recording). Several
> P1/P2 precision fixes are folded in too (sealing library, status-column type, precedent
> citations, OAuth scope wiring, CF-policy construction). See inline `<!-- r1: ... -->` markers
> for exactly what changed and which reviewer(s) found it.
>
> **Provenance:** this spec adapts an existing ARB Memory design-decision-record artefact
> (`art-29c42055ea6e41f0`, authored 2026-07-02 by a claude.ai design session, explicitly marked
> "author is non-quorum, to be verified by decorrelated panel" in its own text) into this repo's
> spec-doc convention, and grounds its implementation details in the concrete conventions already
> established by the two sibling ARB planes (`src/arb_files/`, `src/arb_email/`) rather than
> leaving them abstract. The original artefact's reasoning (§2-9 below largely mirror it) is
> preserved; the concrete "how this fits the codebase" detail (schema style, config convention,
> audit convention, MCP door-wiring convention, testing convention, containment pattern) is new
> in this pass, sourced from a direct read of `arb_files`/`arb_email`'s actual code.
>
> **Security-critical component — read before implementing.** This plane brokers real cloud
> provider credentials (Cloudflare API tokens today, DigitalOcean later). Get the review right;
> this is exactly the class of change `AGENTS.md`'s "protected instruction files" caution exists
> for the *reason* behind, even though this isn't literally on that list — credential-issuing
> code deserves the same scrutiny discipline.

## Problem

Coding agents run on multiple boxes (the mac-mini, remote seats, future hosts). Some of their
work legitimately needs to touch cloud infrastructure — creating a scoped Cloudflare API token,
provisioning a DigitalOcean droplet. Today the only way to do that is for a human to hand an
agent (or the box it runs on) a real, standing credential — which means every box that might
ever need cloud access becomes a place a leaked/compromised credential could do real damage,
and there's no per-request audit trail of *why* a credential was used.

**ARB Messages** is a fourth ARB plane (alongside Memory, Files, Email) that lets an agent
*request* a privileged action instead of *holding* the credential to perform it. A **privileged
executor** process (running wherever the real root credentials already live — the mac-mini)
claims the request, checks it against a policy allowlist, and either mints a scoped short-lived
credential or performs the action itself and returns only the result. The root credential never
leaves the executor's process.

## Scope of this pass

**Cloudflare `mint` only.** The generic queue/envelope/audit machinery is built once and is
provider/request-type-agnostic by design (§4), but the only *handler* wired up in this pass is
`mint` for Cloudflare token creation. `proxy` (executor performs the action itself) and
DigitalOcean support are explicitly deferred — the queue's generic shape means adding them later
doesn't require touching this pass's rails, only adding a new handler. Don't build DO or `proxy`
as part of this implementation; the plan will not include tasks for them.

## Why this is a queue problem, not a store problem

ARB Memory/Files are content-addressable state with loose ordering — search/fetch by meaning or
ID. ARB Messages is the opposite: a **work queue** with strict semantics (enqueue → claim
exactly once → act → report), needing claim/lease/retry/dead-letter and an auditable trail of
who requested what and what was granted. This is why it's a new plane, not a new ARB Memory
artefact type.

## Architecture

### Transport: Postgres, `SELECT ... FOR UPDATE SKIP LOCKED`

Reuses the existing ARB Memory Postgres instance — no new infrastructure. The `arb_messages`
table row **is** the audit-of-record (the request, its scope, the claiming worker, the policy
decision, the result, and every timestamp, in the same transactional store), which matters
specifically because this plane's entire job is privileged credential operations — the audit
trail is the point, not a bolt-on.

Rejected: Redis/Valkey Streams (weaker durability for credential-minting than Postgres, and
every request/result would need mirroring into Postgres for audit anyway — no latency need
justifies the extra failure domain); DO Spaces/object storage (that's the Files plane's job,
wrong tool for ordered claimable work items).

**Schema convention — match `arb_memory/run.py:89-166`'s `setup_schema(conn)` pattern exactly**
(the only existing Postgres-table precedent in this codebase; `arb_files`/`arb_email` have no
Postgres tables of their own, so there's nothing to reconcile there):
- Idempotent `CREATE TABLE IF NOT EXISTS`, `bigserial PRIMARY KEY`, `text`/`timestamptz`/`jsonb`
  column types.
- New columns land as separate, additive `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements
  (supports re-running `setup_schema` on every process start — this codebase has no dedicated
  migration runner; each plane's own `run.py` issues its own DDL on startup, idempotently).
- `CREATE INDEX IF NOT EXISTS` on hot lookup columns — status (for claim queries; see the
  compound `(status, claimed_at)` index noted at § Request lifecycle). <!-- r3 (agy-print P2):
  round-1/2 also called for a standalone index on the requesting-agent column, for per-agent
  audit queries — but the compound `UNIQUE (agent_id, request_id)` constraint below already
  creates a B-tree index on `(agent_id, request_id)`, and Postgres can use a compound index's
  leftmost column(s) alone (a query filtering only on `agent_id` uses this index same as a
  dedicated one would). A separate single-column `agent_id` index would be redundant — dropped.
  --> No separate `agent_id` index needed.
- A `UNIQUE` constraint on the request's dedup key — <!-- r2 (cold-Opus P1-1): compound
  `(agent_id, request_id)`, not a bare client-supplied `request_id`, so one agent's request space
  can never collide with or address another agent's (§ Containment's ownership-check paragraph)
  --> `(agent_id, request_id)`. Mirrors `arb_memory/run.py`'s `stream_entry_id` UNIQUE-as-dedup
  pattern in spirit, needed here for idempotent execution (§ Security invariant 8); doubles as
  the per-agent audit-query index per the point above.
- No repo-wide `arb_`-table-name-prefix convention was found (existing tables are plainly named,
  e.g. `eval_event_raw`) — `arb_messages`/`arb_agent_keys` as names are fine on their own merits
  (descriptive, no collision risk), this isn't a deviation worth reconsidering.
- <!-- r3 (codex, agy-print — the deadletter table needed for the round-3 P0's fail-closed
  path); r4 (codex P1, agy-print P1, cold-Opus P1 — 3-way convergence): round-3 only specified
  the table's write path (a fenced-out worker inserts into it), never its read/drain path — the
  sweep's own spec (below) enumerated two `arb_messages` categories and never actually touched
  this table, despite this paragraph's own round-3 claim that it did. A written-but-never-drained
  deadletter table is a promise without a mechanism: the orphan is recorded but nothing ever acts
  on the record, so it accumulates on Cloudflare indefinitely rather than being reclaimed. Fixed
  by adding a real completion column, mirroring `arb_messages`' own `token_revoked_at` pattern
  for consistency, and giving the sweep an explicit branch for this table (see § Security
  invariant 8's revocation sweep).
  r5 (agy-print P1): the r4 schema had no way to distinguish "not yet retried" from
  "repeatedly failing" — a deadletter entry whose revoke keeps failing for a *permanent* reason
  (e.g. the restricted token's delete permission was revoked entirely, or the token ID is
  malformed) would be re-selected and re-attempted on literally every sweep iteration forever,
  spamming the CF API and the logs with no backoff. Added `attempts`/`last_attempt_at` so the
  sweep can distinguish and back off. --> **`arb_messages_deadletter`** — a small table
  (`provider_token_id text PRIMARY KEY`, `discovered_at timestamptz`, `reason text`,
  `token_revoked_at timestamptz` nullable, `attempts int NOT NULL DEFAULT 0`,
  `last_attempt_at timestamptz` nullable) for an orphaned CF token ID that a stale, fenced-out
  worker (or a revoke-then-remint step, per the P0 fix above) couldn't revoke and can't attach to
  the original row (a different worker already won and completed it, or the row moved on) — see
  § Containment executor-side step 6's fencing guard. Distinct from `arb_messages_settings` (the
  pause flag): this table exists purely so an orphaned token ID is never dropped on the floor
  even when there's no live row left to record it against. Rows are **never deleted** — matching
  this codebase's row-as-audit-record instinct (§ Audit) — a swept, revoked deadletter entry
  stays as a permanent record that a leak happened and was cleaned up, with `token_revoked_at`
  as the completion marker. The sweep (§ Security invariant 8's revocation sweep) increments
  `attempts` and sets `last_attempt_at` on every attempt (success or failure — this is
  observability, not a gate); past a small fixed ceiling (e.g. 10 attempts) the sweep's own
  per-iteration cadence naturally throttles retries to roughly once per sweep cycle rather than
  hammering the API, and the row stays visible (never deleted) as a permanent, growing-`attempts`
  signal that this specific token needs operator attention, distinct from a normal
  freshly-deadlettered entry the sweep will likely clear on its next pass. No hard cutoff that
  stops retrying entirely — an eventually-fixed permission or a transient outage should still let
  the sweep succeed on a later pass — just enough signal for an operator to notice a
  stuck entry via `attempts`/`last_attempt_at` rather than it blending in with fresh ones.

### Live code location: new `src/arb_messages/` package, mirroring `arb_files`/`arb_email`

```
src/arb_messages/
  __init__.py
  config.py       # Settings dataclass + load_settings(env) — see § Config below
  run.py          # setup_schema(conn) — the two tables' DDL
  store.py        # enqueue / claim / ack / result-write against Postgres
  audit.py        # default_audit_sink(event: dict) — log-based, see § Audit below
  keys.py         # agent public-key registration/lookup (arb_agent_keys table access)
  executor.py     # standalone claim/mint/sweep loop — see § Containment (executor-side)
  mint_cloudflare.py  # the one handler this pass builds: CF token minting
  mcp/
    __init__.py
    door_wire.py  # register_messages_tools(server, env) -> bool, wired into arb_memory.mcp.server
    door_tools.py # MessagesTools class — the door-side checks (see § Containment below)
tests/arb_messages/
  test_config.py
  test_run.py
  test_store.py
  test_keys.py
  test_mint_cloudflare.py
  test_door_tools.py
  test_door_wiring.py
  test_executor.py
```

<!-- r1: dropped `mcp/local_server.py` from this pass entirely. Cold-Opus's P0-1 evidence
specifically flagged `arb_email/mcp/local_server.py:8-18` as an unsafe precedent to mirror here
— it builds tools with `require_scope=lambda _scope: None` and a caller-supplied
`actor=lambda: seat_id`, which is fine for ARB Email (containment there is a fixed recipient
allowlist, not identity) but would directly reopen the agent_id-spoofing hole this revision just
closed if copied for ARB Messages. Enqueue/register/poll must all flow through the
OAuth-authenticated door — no fleet-local, scope-free surface for this plane. If a local-fleet
convenience surface is wanted later, it needs its own actor-binding design, not a copy of
`arb_email`'s. Added `executor.py` — round-0 didn't name where the claim/mint loop and the
token-revocation sweep (§ Testing / Risks) actually live; this is that module. -->

**Postgres write access to `arb_messages`/`arb_agent_keys` must be restricted to the door and
executor processes' own DB roles** — <!-- r1 (cold-Opus P0-1, alternative 3): a box that holds
a general-purpose Postgres DSN to this database could otherwise write a self-approved-looking
row directly, bypassing the door entirely. This is a deployment/role-grant requirement, not
application code, but it's load-bearing for the trust model above and belongs in the plan's
verification checklist, not left implicit. -->

Wire into the shared door exactly like the existing two planes
(`arb_memory/mcp/server.py:8-9,383-384`):
```python
from arb_messages.mcp.door_wire import register_messages_tools
...
register_messages_tools(server, os.environ)
```

### Config — `Settings` frozen dataclass + `load_settings(env)`, matching `arb_email/config.py`

```python
@dataclass(frozen=True)
class Settings:
    postgres_dsn: str
    cf_minting_token: str          # the CF token whose ONLY power is creating other tokens (§9)
    allowed_zones: frozenset[str]  # CF zone IDs this executor may mint tokens scoped to; required, non-empty
    allowed_agents: frozenset[str] # agent IDs permitted to request mint at all; required, non-empty
    lease_seconds: int = 300       # visibility timeout before a claimed-but-not-acked request is reclaimable
    delivered_grace_seconds: int = 3600  # r2 (agy-print F2/pi-GLM F2/cold-Opus P2-1): sweep window for done-but-unreceived orphans, see § Security invariant 8's sweep
    max_retries: int = 3
    messages_enabled: bool = True  # kill switch, see below

def load_settings(env: Mapping[str, str]) -> Settings: ...
```

<!-- r2 (cold-Opus P2-2): round-1 said "postgres_dsn and cf_minting_token are required; the rest
have defaults" while also saying allowlists are "validated non-empty at load" — contradictory,
since an unset allowlist would default to empty and empty can't pass non-empty validation.
Resolved: `allowed_zones`/`allowed_agents` are REQUIRED (no default, `_REQUIRED` list per the
corrected `arb_files` precedent above), validated non-empty with their own named error, distinct
from `postgres_dsn`/`cf_minting_token`'s required-and-nonstructural check. An operator who wants
to run this plane with nothing allowlisted yet should set an explicitly-empty-but-present value
that the config layer recognizes as "deliberately deny-all" (e.g. a single sentinel token) rather
than triggering a missing-config error that reads as a deployment mistake — but the default
behavior without that sentinel is fail-to-start, not silently-defaults-to-empty-and-denies-
everything, which would look like a working deployment that mysteriously denies every request. -->

- Env var prefix: `ARB_MESSAGES_*` (matching the `ARB_<PLANE>_*` convention — `ARB_MESSAGES_
  POSTGRES_DSN`, `ARB_MESSAGES_CF_MINTING_TOKEN`, `ARB_MESSAGES_ALLOWED_ZONES` (comma-split),
  `ARB_MESSAGES_ALLOWED_AGENTS` (comma-split), `ARB_MESSAGES_ENABLED` (`"1"`/`"0"`, default on
  — matching `arb_email`'s `send_enabled` string-`"1"`-comparison kill-switch pattern exactly).
- Required-var checking mirrors **`arb_files/config.py`'s** pattern (<!-- r1: corrected —
  codex and cold-Opus both independently found this citation wrong; the joined
  `"<Plane> config missing: ..."` multi-var message is `arb_files/config.py:25-42`'s
  `_REQUIRED`-list pattern, not `arb_email/config.py`'s, which raises a single-var
  `"ARB_EMAIL_POSTMARK_TOKEN required"` message. Use the `arb_files` shape since ARB Messages
  has multiple required vars (`postgres_dsn`, `cf_minting_token`). -->): collect all missing
  required vars and raise `ValueError("ARB Messages config missing: <comma-joined var names>")`
  in one message, not a silent default or a fail-on-first-missing-var loop. <!-- r2 (cold-Opus
  P2-2): required list widened — see the box above this bullet. --> `postgres_dsn`,
  `cf_minting_token`, `allowed_zones`, and `allowed_agents` are all required (the latter two also
  separately validated non-empty, per the next bullet); `lease_seconds`, `delivered_grace_seconds`,
  `max_retries`, and `messages_enabled` have defaults.
- Allowlists validated non-empty and normalized (lower-cased where case-insensitive, e.g. agent
  IDs) at load time, matching `arb_email/config.py:34-44`'s validate-once-at-load pattern (this
  citation checked out — pi-GLM and cold-Opus both verified it independently) — not re-validated
  per call.

### Audit — two layers, not one (a deliberate divergence from the `arb_email` pattern, explained)

<!-- This is the one place this spec explicitly diverges from the sibling-plane pattern, and
says why, rather than silently doing something different. -->

`arb_email`'s `default_audit_sink(event: dict)` is a **log-only** side channel
(`logging.getLogger(...).info(...)`), injected as a callable, never itself the durable
audit-of-record — the durable record there is whatever the email provider's own delivery logs
show. **ARB Messages needs the Postgres row itself to be the audit-of-record** (§ Transport,
above) — a log line is not durable/queryable/transactional the way a credential-issuance audit
trail needs to be. So ARB Messages keeps **both**:

1. An injectable `audit_sink: Callable[[dict], None]` matching the established convention
   exactly (log-based, `default_audit_sink` logs via `logging.getLogger("arb_messages.audit")`,
   never raises — matches `arb_email/audit.py:10-11`'s shape byte-for-byte, just a different
   logger name) — for the same fast, greppable, external-log-shipping observability every other
   plane gets.
2. The `arb_messages` row's envelope columns (§ Two-plane row split) as the durable,
   queryable, transactional record — written by `store.py` as part of the same transaction that
   changes the row's status, not by the audit sink.

Both fire on every state transition (enqueued, claimed, denied, done, failed) — the sink for
fast observability, the row update for the record that actually matters if the two ever
disagree. `store.py`'s state-transition functions call the audit sink internally (matching
`EmailTools`'s pattern of the containment logic owning both the check and the audit call, not
leaving it to the caller — see § Containment).

<!-- r1: codex found the never-raises framing overstated the precedent — `arb_email/audit.py:
10-11`'s `default_audit_sink` itself does not catch exceptions; the never-raise property is
`EmailTools`'s wrapping (`arb_email/mcp/door_tools.py:44-70`'s `try/except` around each call
site), not the sink. Separately, `arb_files/audit.py:13-20` documents the OPPOSITE policy for
destructive file ops — audit exceptions propagate there, deliberately, so a broken audit sink
blocks the destructive action rather than silently letting it through unaudited. ARB Messages'
own policy, stated explicitly rather than borrowed by citation: the injectable `audit_sink` call
is always best-effort/wrapped-never-raise (matching how `EmailTools` wraps it, applied
correctly this time — the sink itself doesn't need to be exception-safe, the caller wraps it),
because the durable record is the Postgres row, not the sink; but the row write itself (the
transactional state-transition in `store.py`) is never best-effort — a failed row write must
propagate and abort the transition, matching `arb_files`' fail-closed instinct for anything
that's actually load-bearing rather than observability. -->

### Two-plane row split (envelope vs. body)

Do **not** encrypt whole rows. Split every `arb_messages` row into:

- **Envelope columns — always cleartext.** Requesting agent, `request_type`, `provider`, scope
  requested, policy decision + reason, timestamps, status, and critically `provider_token_id`
  (the *ID* of a minted token, for revocation — not the token itself). This is the audit log;
  none of it is secret.
- **Body column (`bytea`, the result payload) — sealed, structurally enforced, not a
  convention.** <!-- Plan-review r2 (agy-print P2): corrected from `jsonb` — the implementation
  plan (`docs/superpowers/plans/2026-07-02-arb-messages-cloudflare-mint.md`) found during its own
  round-1 review that raw PyNaCl `SealedBox` ciphertext is opaque bytes, and psycopg3 cannot
  insert `bytes` into a `jsonb` column; `bytea` is correct for opaque sealed ciphertext that's
  never queried/indexed as JSON. r1: rewritten. Round-0's "executor-decides-by-payload-class" framing was
  right in spirit but under-specified two different things: codex (P1) found nothing actually
  *prevents* a bug from writing a cleartext secret into the body column — the safety was still
  "the handler must remember to route the secret through the seal function," i.e. a convention,
  not a guarantee. pi-GLM separately noted (P2, design-hygiene) that for *this pass* — `mint`
  is the only handler, and a mint result is *always* a secret — the payload-class dispatch
  branch has no code path that should ever choose cleartext, so building the dispatch logic
  preemptively is pure surface area for exactly the bug codex is worried about. Combining both:
  simplify to unconditional sealing for this pass's one handler, AND make it structural rather
  than conventional. --> `store.py` exposes exactly one write path for a `mint` result:
  `write_sealed_result(row_id, sealed_bytes: bytes, provider_token_id: str)` — there is no
  function in this codebase's `mint`-handling path that accepts a plaintext token as an
  argument to a row-writer. `mint_cloudflare.py`'s handler calls `seal(token, recipient_pubkey)`
  and passes only the resulting ciphertext to `write_sealed_result`; the raw token never reaches
  `store.py` at all. A DB-level `CHECK` (or, if Postgres CHECK can't easily express it, a
  store-level assertion enforced on every write) requires that a `done` row with
  `request_type='mint'` has a non-null body — <!-- r2 (cold-Opus P2-3): stated more precisely —
  round-1 described this CHECK as "ensuring a mint can't complete without going through the seal
  step," which over-reaches what a presence check can actually prove. The CHECK only guarantees
  *some* body exists, not that it's genuinely sealed ciphertext rather than, say, a mistakenly-
  unsealed JSON blob a future bug might write; a CHECK can't inspect whether bytes are ciphertext
  versus plaintext. The actual no-plaintext guarantee is entirely the single-write-path
  signature (`write_sealed_result` takes `sealed_bytes`, no code path constructs it from
  anything but `seal()`'s own output) — --> a mint can't complete with a null body, and separately
  (by construction of the write path, not by anything the CHECK itself inspects) can't complete
  with an unsealed one. The payload-class dispatch branch pi-GLM flagged as premature is **not
  built this pass** — it's added when a second handler that legitimately returns non-secret
  cleartext (a future `proxy` handler) actually exists, not preemptively for a class of message
  that
  doesn't exist yet.

Why sealed-by-construction rather than the alternatives round-0 considered: unconditional
encryption of the *whole row* blinds the audit trail (defeats the entire reason this plane is on
Postgres — the envelope columns stay cleartext, only the body is sealed); caller-flagged
encryption rots (someone forgets the flag, a secret lands in the clear); a convention that "the
handler is supposed to seal secret results" rots the same way once a second handler exists and
someone copies the wrong branch. Structural — no code path from raw secret to row-writer that
skips sealing — is the version that can't rot. Mirrors the deny-path discipline already
established for ARB Email: the safety behavior is enforced at the boundary, not requested
politely by the client (or, here, by the handler's own good behavior).

### Request lifecycle & handler shape

`request_type` is a discriminator selecting a handler. This pass implements exactly one:
`mint` for Cloudflare. The transport (claim/ack/lease/retry/dead-letter/audit) is entirely
handler-agnostic — a future `proxy` handler or a DO handler adds a new function, not a change to
`store.py`'s queue machinery.

Status: <!-- r1 (agy-print Finding 9): plain `text` column with the allowed values enforced in
Python, not a native Postgres `ENUM` type — matches the `eval_event_raw`/`transcript_io`
precedent (`arb_memory/run.py`) and avoids `ALTER TYPE ... ADD VALUE`'s well-known idempotency
headaches (can't run inside the same transaction as other DDL, no `IF NOT EXISTS` form) if a
status value is ever added later. --> `pending → claimed → done | failed | denied`, plus
`delivered_at` (nullable timestamp, § Containment's exactly-once-retrieval fix) once a `done`
row's result has been read. `claimed_at` + `lease_seconds` (§ Config) gives the
visibility-timeout semantics — a claimed-but-never-acked row becomes reclaimable after the lease
expires (standard `SELECT ... FOR UPDATE SKIP LOCKED` queue pattern:
`WHERE status = 'pending' OR (status = 'claimed' AND claimed_at < now() - lease_interval)`).
<!-- r1 (cold-Opus P2-3): the claim query's `WHERE` clause wants a supporting compound index on
`(status, claimed_at)`, not just a bare index on `status` — add both to `run.py`'s
`CREATE INDEX IF NOT EXISTS` list. -->

<!-- r5 (agy-print P1): `Settings.max_retries` (§ Config) was defined but never actually wired
into anything — no schema column tracked attempts, no claim-time check enforced the limit. A
"poison pill" request (one whose payload reliably crashes or hangs the executor on every claim,
e.g. a malformed capability term that survives the door's pre-check but breaks the executor's
policy-construction step) would be claimed, fail/hang/get reclaimed, and claimed again
indefinitely — an unbounded crash-loop with no mechanism ever stopping it, which for an executor
process handling real credentials is a genuine availability/DoS-on-self concern worth closing. --> **A claim-attempt counter closes this.** Add an `attempts int NOT NULL DEFAULT 0` column.
The claim query increments it as part of the same atomic claim (`UPDATE ... SET status='claimed',
claimed_at=now(), attempts=attempts+1 WHERE id = (SELECT id FROM arb_messages WHERE ... FOR
UPDATE SKIP LOCKED LIMIT 1) RETURNING *`, or equivalent). Before minting, the executor checks
`attempts > Settings.max_retries`; if exceeded, the row is marked `failed` with a
"max retries exceeded" reason instead of being processed — this is a normal, audited terminal
state (§ Audit), not a special case. This bounds the crash-loop at `max_retries` attempts and
gives an operator a clear signal (a `failed` row with that specific reason) rather than a
silently-repeating claim/reclaim cycle.

### `arb_agent_keys` — public-key self-registration, bound to the authenticated actor

<!-- r1: this whole section rewritten. Round-0 said "agents self-register" without ever stating
what binds a registration's `agent_id` to a real identity — all four reviewers independently
found this exploitable (codex P0, pi-GLM F2/P1, agy-print P0-2, cold-Opus P0-2): an
unauthenticated or caller-supplied `agent_id` lets an attacker register a key for (and receive
mints intended for) any allowlisted agent, either by racing the legitimate agent's first
registration or overwriting an existing live key. Fix: `agent_id` is NEVER a caller-supplied
field, anywhere in this plane — it is always derived server-side from the authenticated OAuth
actor, exactly once, at the door. -->

New table, not ARB Memory (Memory is semantic/searchable content — wrong plane for trust
material, same instinct as keeping root tokens off the boxes). Columns: `agent_id`, `pubkey`,
`fingerprint`, `created_at`, `revoked_at`.

**Identity binding (the load-bearing rule):** `messages_register_key` (the MCP tool) takes only
a `pubkey`; it never accepts an `agent_id` parameter at all. The door derives `agent_id` from
`_actor()` (the authenticated OAuth `client_id`, matching `arb_email/mcp/door_tools.py:38-42`'s
`_default_actor()` *pattern only, not its `or "mcp"` fallback — see § Containment's door-side
step 2 for the fail-closed divergence* <!-- r2 (agy-print, cold-Opus): back-reference so the
divergence is visible from wherever a reader first encounters `_default_actor()`. -->) and that
is the only value ever written to the column. This closes the impersonation vector at the source
— there is no code path where a caller can name a different agent's identity, so "race the
legitimate agent's registration" and "overwrite a different agent's key" both require *already
controlling that agent's OAuth-authenticated identity*, which is a different (and
already-assumed-defended) boundary. <!-- r2 (cold-Opus P2-4): this whole binding is only as
strong as two assumptions worth stating explicitly rather than leaving implicit: (a) OAuth
Dynamic Client Registration *assigns* `client_id` server-side — a registering client cannot
choose or request a specific `client_id` — and (b) `Settings.allowed_agents` is curated to match
the actual assigned `client_id`s an operator wants to permit, not some other identifier a client
might claim in-band. Both hold under this server's current DCR configuration
(`arb_memory/mcp/server.py`), but if that configuration ever changed to accept client-requested
IDs, this entire identity-binding story would silently stop being what it appears to be — worth
a comment at the `_actor()` call site pointing back here, so a future DCR change is forced to
notice this dependency. -->

**First-registration-wins, no self-service rotation this pass:**
`CREATE UNIQUE INDEX ON arb_agent_keys (agent_id) WHERE revoked_at IS NULL` (one live key per
agent, enforced by the database, not just application logic) — a first `INSERT` for a given
`agent_id` succeeds; a second attempt from the same already-registered agent is rejected by the
unique index, not silently overwritten. <!-- r2 (agy-print P2/F-05): first-registration-wins
makes a bad first registration unusually costly — if an agent's registration is malformed (bug,
truncated write, wrong key format) it is locked in until an operator manually clears it, since
there's no self-service rotation this pass (below). `messages_register_key` therefore validates
`pubkey` is a well-formed PyNaCl public key (correct byte length, decodable) *before* the
`INSERT`, rejecting a malformed key at registration time with a clear error rather than locking
in garbage. This doesn't reopen the rotation question — it just stops an obviously-bad key from
ever occupying the one slot the unique index protects. --> **Key rotation is explicitly out of
this pass's scope:**
if an agent's key needs rotating (suspected compromise, routine hygiene), that requires an
operator action (a manual `revoked_at` update, freeing the agent to re-register) — no
self-service "replace my key" tool is built this pass. This is a deliberate scope cut, not an
oversight: self-service rotation needs either proof-of-possession of the old private key
(signature verification machinery) or an equivalent trust mechanism, and building that isn't
justified for this pass's single Cloudflare-mint handler. Revisit if/when rotation friction
becomes a real operational problem.

An agent cannot receive a `mint` result until it has published a public key — this is an
**invariant**, not an optional nicety. Because of the door/executor split (§ Containment below),
this is checked **twice**: once at the door (fail-fast UX — reject enqueue immediately if no
live key exists yet) and again, authoritatively, by the executor immediately before minting
(the door-time check is necessarily stale by claim time — see § Containment's TOCTOU note).

Sealing scheme: <!-- r1: changed from `age` to PyNaCl, per agy-print's Finding 5 — `age` is a
Go CLI binary, not installed by default on macOS/Linux worker hosts, and using it means either
shelling out to an external binary or a Python `age` binding with weaker maintenance than
mainstream crypto libraries. --> **PyNaCl's `SealedBox`** (pure-Python-installable via the
existing `.venv`/`uv` toolchain, no external binary dependency, a well-established library for
exactly this "seal a message to a recipient's public key, only the recipient's private key can
open it" pattern — the right tool for sealing a mint result to the requesting agent's registered
key).

### Containment — a door/executor split, not a single synchronous check sequence

<!-- r1: this whole section rewritten. Round-0 said "MessagesTools implements the same check
sequence EmailTools already establishes" and listed 5 steps as if they ran in one place. All
four reviewers independently rejected this (pi-GLM F1/P0, agy-print P0-1, cold-Opus P0-1, and
codex's two P0s are consequences of the same gap): ARB Messages is explicitly a two-process
async queue, and the two processes have fundamentally different capabilities —
- **The door** (inside the shared MCP server, `arb_memory/mcp/server.py`) has a real MCP
  request context and can call `get_access_token()` to get an authenticated actor.
- **The executor** (a standalone process that claims rows from Postgres) has NO MCP request
  context at all — `get_access_token()` is not just inconvenient there, it's meaningless,
  because the executor isn't serving an MCP request when it claims and mints. It reads a row a
  door process wrote, arbitrarily long ago.

Treating `EmailTools`'s single-process check sequence as the template for a two-process design
produces code that either can't run (the executor calling an OAuth accessor with no request
context) or silently skips the re-validation a credential minter actually needs (agy-print's
framing: a compromised or buggy door, or a box that holds raw Postgres write access to
`arb_messages`, could write an already-"validated"-looking row directly — the executor MUST NOT
trust that a row's stored policy decision is real just because the row exists). The fix is to
name each check's site explicitly, and make the executor independently re-derive its own
allow/deny decision from its own config, never from anything the row claims about itself. -->

**Door-side (enqueue) — `MessagesTools` in `door_tools.py`, matching `EmailTools`'s general
shape (`arb_email/mcp/door_tools.py:16-29`) for what it's actually good for: fast-reject UX and
turning an OAuth-authenticated caller into a `pending` row.**

1. **Scope check** — OAuth scope `messages.request` gates the ability to call
   `messages_request`/`messages_register_key` at all. <!-- r1 (codex P1): this scope must be
   added to `src/arb_memory/mcp/server.py`'s hard-coded `valid_scopes`/`default_scopes` lists
   (currently memory/files/email only, `arb_memory/mcp/server.py:323-330`) — this is a required
   plan task, with a regression test matching `tests/arb_email/test_door_wiring.py:59-77`'s
   pattern of asserting the new scope is in both lists. -->
2. **Actor extraction** — `agent_id = _actor()`, the authenticated `client_id`. Never accepted
   as a caller-supplied parameter anywhere in this plane (§ `arb_agent_keys`, above). <!-- r2
   (agy-print, cold-Opus P1-2 — 2-way convergence): `_actor()` matches `arb_email/mcp/door_tools.
   py:38-42`'s `_default_actor()` *shape* (pull `client_id` off the access token) but must
   **not** inherit its `or "mcp"` fallback (`return getattr(token, "client_id", None) or "mcp"`).
   That fallback is harmless for ARB Email (containment there is a fixed recipient allowlist,
   not identity), but on a plane where `agent_id` gates minting, key registration, and the seal
   recipient, an absent `client_id` collapsing every unauthenticated-somehow caller into one
   shared `"mcp"` bucket would corrupt first-registration-wins and exactly-once semantics, and if
   `"mcp"` were ever accidentally in `allowed_agents` it would mint unauthenticated. ARB
   Messages' `_actor()` must **raise/deny** (fail closed) on a missing `client_id`, never
   default to a shared identity — this is a genuine, deliberate divergence from the cited
   precedent's exact behavior, not a copy of it. -->
**Ownership check on the read path (`messages_poll`), not part of the enqueue sequence above:**
<!-- r2 (codex, agy-print, cold-Opus — 3-way convergence): round-1 specified
`messages_poll(request_id)` as a fallback for the slow path (§ Containment's usability note,
below) but never registered it explicitly in `door_wire.py` or stated an authorization check on
it. Because `request_id` was a bare, globally-unique, client-supplied value reused as the CF
token name, any caller holding `messages.request` scope could poll (and thereby consume the
one-time `delivered_at` slot of) another agent's request — a DoS against the legitimate owner,
and (per cold-Opus) a *collision*, not just a guess: two agents choosing the same low-entropy
`request_id` would dedupe onto the same row even without any malicious intent. Fixed by
namespacing the dedup key AND requiring ownership: --> the enqueue dedup key (§ Transport's
`UNIQUE` constraint) is `(agent_id, request_id)`, not a bare `request_id` — one agent's request
space can never collide with another's. `messages_poll(request_id)` is registered explicitly
alongside `messages_request`/`messages_register_key` in `door_wire.py`, requires
`messages.request` scope (same as the others), and its `SELECT`/`UPDATE` is scoped to
`WHERE agent_id = _actor() AND request_id = $1` — a poll for a `request_id` that exists but
belongs to a different actor returns "not found" (not "forbidden," to avoid confirming another
agent's request even exists), and can never mark a row it doesn't own as delivered. The same
`(agent_id, request_id)` compound key is what the deterministic CF token name is derived from
(§ Security invariant 8), not the bare `request_id`.

3. **Allowlist pre-check** — is `agent_id` in `Settings.allowed_agents`? Fast, useful UX
   (reject an obviously-disallowed caller immediately, don't make them wait for the executor to
   deny it), but **not the authoritative decision** — see executor-side, below.
4. **Agent-key pre-check** — does `agent_id` have a live key yet (§ `arb_agent_keys`)? Same
   fast-UX-not-authoritative status as the allowlist pre-check.
5. **Kill-switch check** — `register_messages_tools` returns `False` fast if
   `Settings.messages_enabled` is unmet at registration (matching `arb_email/mcp/door_wire.
   py:9-36`'s fail-fast-and-never-crash-the-shared-door pattern — this part of the round-0 claim
   checked out).
6. **Enqueue** — write a `pending` row with `agent_id` (from step 2), `request_type='mint'`,
   `provider='cloudflare'`, and the request shape <!-- r2 (pi-GLM F3): round-1 mentioned only
   "capability" as the enqueued field, but executor-side step 1 (below) already referred to "the
   row's requested zone" — pinning the actual shape closes that gap and adds the missing
   door-side pre-check. --> **`{capability, zone}`** — `capability` a small allowlisted
   vocabulary term (e.g. `zone_dns_edit`, matching executor-side step 2's vocabulary exactly —
   the door and executor share one capability-term enum, not two independently-maintained
   lists), `zone` a Cloudflare zone ID. **New door-side pre-check:** is `zone` in
   `Settings.allowed_zones`? (Same fast-UX-not-authoritative status as the allowlist/key
   pre-checks — the executor re-derives this independently regardless, per executor-side step 1.)
   Neither field is ever a caller-supplied CF policy blob — see executor-side step 2, below.
   Deny-with-audit for any pre-check failure in steps 3-4 or the new zone pre-check;
   `_deny`/`_audit_failed` both wrapped `try/except Exception: log.exception(...)` so a broken
   audit sink never itself breaks the deny path (this part of the round-0 citation also checked
   out — `arb_email/mcp/door_tools.py:44-70`).

**Executor-side (claim + mint) — a standalone process, no MCP/OAuth context, treats every
claimed row as untrusted input and independently re-derives its own decision.**

1. **Re-check allowlist against the executor's own loaded `Settings`** — is the row's
   `agent_id` in *this process's* `allowed_agents`, and the row's requested zone in *this
   process's* `allowed_zones`? Never trust the row's own `policy_decision` column as if it were
   already authoritative — that column records what the door *thought*, for audit purposes; the
   executor computes its own answer independently. A compromised door, a compromised box with
   direct Postgres write access, or a stale door config must not be able to buy a mint by
   writing a pre-approved-looking row.
2. **Construct the CF policy from the row's capability term, never pass through row data
   verbatim** — <!-- r1 (pi-GLM F5): the executor's own code maps a small allowlisted
   vocabulary of capability terms (`zone_dns_edit`, `zone_settings_read`, ...) to the actual CF
   permission-group JSON. The row never carries (and the executor never accepts) a
   caller-supplied policy blob — that would let a crafted "requested_scope" escape the intended
   zone/permission bounds even if the allowlist check above passed on a coarser field. -->
3. **Re-check agent-key liveness at claim time, not just trusting the door's earlier check** —
   <!-- r1 (pi-GLM F6, agy-print Finding 3, cold-Opus): closes the TOCTOU where a key is revoked
   between enqueue and claim. The door's pre-check (above) is necessarily stale by the time the
   executor claims — revocation is exactly the kind of event that can happen in that window. -->
4. **Re-check the kill switch against the executor's own config** — <!-- r1 (agy-print Finding
   8, corrected against `arb_email/mcp/door_wire.py:9-23`, which checks `send_enabled` only at
   *registration* time, not per-call — round-0's claim that this "matches the two-layer
   SEND_ENABLED check arb_email's panel review required" was factually wrong, there is no
   per-call check in arb_email to match). This is a **new decision for ARB Messages**,
   justified on its own merits (a credential minter's kill switch should stop in-flight minting,
   not just new enqueues), not a borrowed precedent. Operationally this means flipping
   `ARB_MESSAGES_ENABLED=0` on the door only stops new enqueues — halting an in-flight
   executor requires either restarting it with the flag flipped, or (better, noted as a plan
   task) the executor re-reading a DB-level pause flag on every claim-loop iteration so a
   single operator action is authoritative regardless of process restart timing. -->
   <!-- r2 (agy-print P2/F-04): round-1 proposed this pause flag but never gave it a schema — a
   required plan task, not just a mention, or it never gets built. -->
   `run.py`'s `setup_schema` adds one more table: `arb_messages_settings (key text PRIMARY KEY,
   value text NOT NULL)`, a plain key-value table (not folded into the `arb_messages` row
   schema, since a pause flag isn't a request and shouldn't share that table's claim/lease
   machinery) — the executor's claim loop does one extra cheap
   `SELECT value FROM arb_messages_settings WHERE key = 'paused'` per iteration, read fresh
   every time, before proceeding to claim.
   <!-- r4 (pi-GLM F4): round-2 offered caching this read with a short TTL as an optimization if
   the extra query proved costly. Dropped — for a credential-plane kill switch, immediate effect
   matters more than the query cost saved by caching, and caching reintroduces exactly the "flip
   the switch, nothing happens for N seconds" confusion this design otherwise goes out of its way
   to avoid (§ door kill-switch wording, below). -->
   An operator sets `UPDATE arb_messages_settings SET value = '1' WHERE key = 'paused'` (or a
   small CLI/tool wrapping that) for an immediate, restart-free, single-action halt.
   <!-- r4 (cold-Opus P2): the row's initial seed, written by `setup_schema` on every process
   start (this codebase's idempotent-DDL convention, § Transport), must be
   `INSERT ... ON CONFLICT (key) DO NOTHING` — not an upsert that resets `value` to `'0'`
   unconditionally. Otherwise an executor restart during an active incident (exactly when an
   operator has set `paused='1'` and exactly when a restart is plausible — a deploy, a
   crash-restart) would silently un-pause the plane the moment `setup_schema` re-runs, defeating
   the pause flag at the one moment it matters most. -->
5. **Mint idempotently, record before completing** — see the rewritten § Security invariant 8,
   below, for the exact mechanism (deterministic CF token naming, revoke-then-remint on a found
   orphan + immediate `provider_token_id` write).
6. **Fence the result-write against lease-reclaim overlap** — <!-- r2 (pi-GLM F1): a distinct
   failure mode from the crash-then-restart case invariant 8 covers. A worker can *hang* (not
   crash) past `lease_seconds` without dying — a slow CF API call, a stuck network read. A second
   worker then legitimately reclaims the same row (§ Request lifecycle's lease-expiry clause) and
   proceeds, while the first worker is still alive and eventually finishes its own attempt,
   unaware it lost the row. Without a guard, the first worker's late completion could overwrite
   the second worker's already-written result, or independently pass through invariant 8's
   lookup-before-create logic concurrently with the second worker and create a genuine race on
   the CF API itself (CF does not enforce token-name uniqueness — two concurrent creates with the
   same name can both succeed, producing two live tokens under one deterministic name).
   r3 (codex P0, agy-print P1, cold-Opus P0 — 3-way convergence with matching failure timelines
   and matching remediation, treated as confirmed): round-2's version of this paragraph claimed
   "a worker that successfully fences its `provider_token_id` write has thereby also confirmed it
   still owns the row before proceeding to the CF mint call itself" — this is chronologically
   impossible and was simply wrong: the CF mint call happens *first* (the executor doesn't know
   the token ID until CF's create response returns it), so the fence check on the
   `provider_token_id` write necessarily happens *after* the mint, not before. This matters
   because it means the fencing guard on its own does NOT prevent the concurrent-double-mint race
   it claimed to close — a genuinely serious gap all three reviewers traced through the same
   concrete timeline: worker A claims, hangs *inside* the CF create call past its lease; worker B
   reclaims, its lookup-before-create runs while A's create is still in flight so it finds
   nothing, B mints its own token and completes normally; A's create then lands, returning a
   *second* live token under the same deterministic name (CF permits duplicate names); A attempts
   its `provider_token_id` write, the fence rejects it (zero rows affected), and per round-2's
   "discard its result, log the lost-race event" instruction, A's token is never recorded
   anywhere — the sweep (which only ever touches rows with a non-null `provider_token_id`) can
   never find or revoke it. That is exactly the "live, unrevokable orphan token" round-1 rated
   P0, reintroduced by the round-2 fencing guard's own gap. Fixed: --> every write the executor
   makes to a claimed row (the `provider_token_id` write, the sealed-result write, any
   `failed`/`denied` write) is conditioned on
   `WHERE status = 'claimed' AND claimed_at = <this worker's own claim timestamp>` — if the
   row's `claimed_at` has moved on (a second worker reclaimed it), the write affects zero rows.
   **A stale worker whose `provider_token_id` write is fenced out after a successful CF create
   already holds that token's ID in memory (from the create response) — it must immediately
   revoke that token via the CF API before stopping, not merely discard and log.** If the
   revoke call itself fails, the worker must record the orphaned token ID somewhere that does
   not depend on owning the original row (e.g. a small deadletter table keyed by
   `provider_token_id`, distinct from `arb_messages`, so the sweep — or an operator — can still
   find and clean it up even though the row it came from was won by a different worker) before
   stopping. The fencing guard narrows the concurrent-double-mint race to a genuinely small
   window (between a worker's own pre-mint fence check and its CF create call returning — CF API
   latency, not the whole lease duration) rather than closing it outright; a worker's stale
   external call completing after it lost the row is exactly the residual this window admits,
   and the revoke-on-fence-failure rule is what keeps that residual from leaking a credential
   instead of just costing an extra CF API round-trip.
7. **Deny-with-audit else seal-and-complete** — same never-raise-from-audit discipline as the
   door side.

`register_messages_tools(server, env, *, client_factory=None) -> bool` in `door_wire.py`
mirrors `register_email_tool`'s exact shape (`arb_email/mcp/door_wire.py:9-36`): check required
env and the kill-switch first and return `False` fast; wrap the whole setup in
`try/except Exception: log.exception(...); return False` so a broken ARB Messages plane never
takes the shared door down; build settings → store → tools; define inner
`async def messages_request(...)`, `async def messages_register_key(pubkey: str) -> dict` (no
`agent_id` parameter — see above), and <!-- r2 (codex): round-1 didn't actually register
`messages_poll` here despite describing it elsewhere. --> `async def messages_poll(request_id:
str) -> dict` (all three, no `agent_id` parameter on any of them) with docstrings that become
the MCP tool descriptions; register all three via `server.add_tool(...)`; return `True`.
<!-- r2 (cold-Opus P1-4): this kill-switch check, matching `arb_email/mcp/door_wire.py:9-23`
exactly, runs **only at registration** — flipping `ARB_MESSAGES_ENABLED=0` on a running door
process does nothing until that process restarts (the tool stays registered and keeps
enqueuing). This is stated explicitly here, rather than implied to be a runtime switch, because
an operator on a credential-minting plane will reasonably expect `=0` to have immediate effect;
it doesn't, on the door side, without either a restart or the executor-side DB-pause-flag (see
executor-side step 4, above, and the new settings-table task below) — the DB-pause-flag is the
only *actually* runtime-effective kill switch this design has. -->

**Usability note (agy-print Finding 7, adopted in simplified form):** rather than a pure
fire-and-forget enqueue that forces the calling agent into a hand-written polling loop,
`messages_request` performs a short bounded internal wait (e.g. up to ~15 seconds, comfortably
inside typical CF mint latency) after enqueueing, polling the row itself server-side. If the
executor completes within that window, return the sealed result inline — good UX, matching the
synchronous feel of `email_send`. If not, return `{"status": "pending", "request_id": ...}` and
the agent falls back to the explicit `messages_poll(request_id)` tool (registered above,
ownership-checked per the paragraph earlier in this section). This avoids both a fully-blocking
call with no timeout (bad if the executor is slow/down) and a mandatory always-poll UX (bad
ergonomics for the common fast case) without needing PG `LISTEN`/`NOTIFY` machinery for this
pass.

**Exactly-once retrieval (§ Security invariant 5):** <!-- r1 (pi-GLM F3): round-0 stated the
invariant but specified no mechanism — a second `messages_poll` call would simply re-read and
re-return the same sealed body. Fix: add a `delivered_at` timestamp column; the result-read path
(`messages_poll` and the inline-wait path in `messages_request`) marks the row delivered in the
same transaction as the read, and a subsequent poll of an already-delivered row returns
`{"status": "already_delivered"}` rather than the body again. r2 (pi-GLM F5): stated more
precisely — this is not merely "both paths do this," it is that **both paths call the exact same
`store.py` function** (a single `read_and_mark_delivered(agent_id, request_id)` that does the
`SELECT ... FOR UPDATE` + ownership check (previous paragraph) + `delivered_at` write in one
transaction) rather than each independently reimplementing "read, then separately mark
delivered" — two call sites sharing one atomic function is what actually prevents an
implementor's inline-wait path from returning the body without going through the same
delivery-marking transaction as the explicit poll path. r4 (agy-print P1): round-2's description
of `read_and_mark_delivered` never gated the `delivered_at` write on the row actually being
`done` — read literally, a poll arriving while the row is still `pending`/`claimed` (the agent
checking in before the executor has finished) would mark `delivered_at` on a row with no sealed
body yet. Once the executor later completes that row, every subsequent poll (including the
agent's own, now-legitimate one) would see `delivered_at` already set and get
`already_delivered` instead of the actual result — permanently losing a token that was never
delivered even once. Fixed: --> `read_and_mark_delivered(agent_id, request_id)` **only writes
`delivered_at` when the row it finds is `status = 'done'` with a non-null sealed body.** For any
other status — `pending`, `claimed`, `failed`, or `denied` <!-- r5 (cold-Opus Finding D): round-4
enumerated only `pending`/`claimed` explicitly; `failed`/`denied` are covered by the same
governing rule (neither is `done`, so neither marks delivery) but weren't spelled out, leaving an
implementor to wonder what a poll of a `failed` row returns. Named explicitly for completeness.
--> — it returns the row's current status (`{"status": "pending"}` / `{"status": "claimed"}` /
`{"status": "failed"}` / `{"status": "denied"}`) **without touching `delivered_at` at all** — a
poll of any non-`done` row must be a true no-op on delivery state, freely repeatable, not a
one-shot check. Only a read that finds a genuinely `done` row marks delivery and returns the
body; only *that* read consumes the one-time slot.

## Security invariants

<!-- r1: 5, 7, 8 rewritten with concrete mechanisms — round-0 stated these as goals without a
design that actually delivers them; all four reviewers independently caught this for 7/8
(codex P0-1, pi-GLM F4/P1, agy-print Finding 4/P1, cold-Opus P1-1), pi-GLM alone caught it for 5
(F3/P1). -->

1. **Trust arrow points outward.** Executor initiates claim/act; boxes never SSH/pull into the
   executor's host. A compromised remote box must not hold a key into the credential anchor.
2. **Request carries intent, never a credential.** Agent states "token with scope X for purpose
   Y"; executor validates against the allowlist, acts, returns a handle/result only.
3. **Tightest scope, bound to purpose.** Out-of-allowlist requests are `denied` with a reason,
   never silently executed.
4. **Shortest TTL the provider allows.** Cloudflare's `expires_on` set as tight as the caller's
   stated need permits.
5. **One-time retrieval.** <!-- r1: mechanism added, see § Containment's exactly-once-retrieval
   fix — a `delivered_at` column set on first read, subsequent polls of a delivered row get
   `{"status": "already_delivered"}` instead of the body. --> A result is claimable exactly
   once — redelivery must not hand the same minted token to two readers.
6. **Consume-and-destroy on the agent side** (out of this pass's scope to enforce server-side —
   documented as the agent-side contract, not code this plane can verify).
7. **Record enough to revoke.** `provider_token_id` is written to the row **immediately** after
   a successful Cloudflare mint call returns — its own small transaction, separate from (and
   before) sealing the body and marking the row `done`. This means even a crash between minting
   and full completion leaves the token ID durably recorded and revocable, not orphaned.
8. **Idempotent execution — at the Cloudflare API level, not just the Postgres row level.**
   <!-- r1: the concrete mechanism codex, pi-GLM, and agy-print each independently proposed a
   version of ("deterministic naming + lookup-before-create" / "reconciliation" / "atomic
   minting state") — this is the synthesis.
   r2 (codex, agy-print — CONFIRMED with cited Cloudflare API docs, not a judgment call): the
   round-1 "adopt the existing token's ID and proceed to seal-and-complete" step is **impossible
   as written**. Cloudflare's token-creation API returns the plaintext token secret exactly once,
   in the create response (developers.cloudflare.com/fundamentals/api/how-to/create-via-api/);
   any subsequent lookup-by-name (list/get) returns only metadata — ID, name, expiry, status —
   never the secret (developers.cloudflare.com/api/resources/user/subresources/tokens/methods/
   list/). If the executor crashes after a successful mint but before persisting the secret,
   finding that a same-named token exists on retry tells it nothing it can act on: there is no
   secret left to seal. "Adopt and complete" was internally consistent with the mechanism's own
   framing but not with what the Cloudflare API actually returns. Fixed by replacing the adoption
   step with revoke-then-remint, below — the only options once a secret is unrecoverably lost are
   "get rid of the orphan and mint a fresh one" or "give up and surface the failure," not
   "pretend the old one is still usable." --> The `UNIQUE` constraint on the enqueue request ID
   (§ Transport) prevents a duplicate *enqueue*, which is necessary but not sufficient: it does
   nothing about the same *row* being minted twice across a lease reclaim, and Cloudflare's
   token-creation API has no idempotency-key support of its own. The executor closes this gap
   itself: **name every minted CF token deterministically from `(agent_id, request_id)`** <!-- r2
   (cold-Opus P1-1, folded into the naming fix below): namespaced per-agent, not a bare global
   `request_id`, so one agent's dedup key can never collide with or address another agent's
   request/token. --> as `arb-msg-{sha256(sha256(agent_id).hexdigest() + ":" +
   sha256(request_id).hexdigest())[:32]}` <!-- r3 (agy-print P2): round-2's formula,
   `sha256(agent_id + ":" + request_id)`, has a separator-injection collision — if `agent_id`
   itself can contain a colon, `agent_id="a:", request_id="b"` and `agent_id="a", request_id=":b"`
   concatenate to the identical string `"a::b"` before hashing, producing the same deterministic
   name for two genuinely different requests. Hashing each component *first*, then concatenating
   the hashes (fixed-length, so no ambiguity about where one component ends and the next begins),
   removes the collision regardless of what characters `agent_id`/`request_id` contain — no
   separate charset validation needed on either field. --> <!-- r2 (codex P2,
   pi-GLM F4 — 2-way convergence): round-1 proposed raw interpolation
   (`arb-messages-{request_id}`), but Cloudflare's token-creation API caps `name` at 120
   characters — a caller-supplied `request_id` (no length constraint stated anywhere in round-1)
   could exceed that and fail the mint at the API level with a confusing error, and raw
   interpolation of caller-controlled input into an API field is also just bad hygiene generally.
   A bounded hex digest of the compound key is deterministic (same inputs always produce the
   same name — required for lookup-before-create to work), fits comfortably under any reasonable
   provider length limit, and needs no separate `request_id` format validation at enqueue (pi-GLM's
   alternative suggestion) since the digest absorbs whatever the caller sends. --> (a fixed-length
   hex digest, well under Cloudflare's 120-character token-name limit). Before calling
   the mint API, the executor **looks up whether a token with that name already exists**. If
   **not** found: mint fresh, then immediately record `provider_token_id` (invariant 7) before
   doing anything else. If **found** — <!-- r3 (pi-GLM F1, cold-Opus — 2-way convergence): round-2
   framed this branch as firing only when "the row's own `provider_token_id` [is not] already
   being set," reasoning that was the only way a same-named token could exist without a sealed
   result. That's false: invariant 7 writes `provider_token_id` in its own transaction,
   *separate from and before* sealing — so a crash landing between that write and the seal step
   leaves a row with `provider_token_id` already set, body still null, and the secret already
   unrecoverably lost (CF returns it exactly once, at creation, same limitation that broke
   round-1's mechanism). Read literally, round-2's wording tells an implementor that a found
   token with `provider_token_id` already set is NOT the revoke-then-remint case — exactly
   backwards, since that's precisely when revoke-then-remint is needed (there is nothing to seal;
   the executor cannot proceed to "seal-and-complete" no matter what it does). Fixed by
   reframing the condition around body-sealed status, the property that actually determines
   whether there's a secret left to work with, not `provider_token_id`'s presence, which doesn't.
   --> a same-named token exists **and** the row is not yet `done` with a non-null sealed
   body (regardless of whether `provider_token_id` is already set on this row — that column's
   state doesn't change what needs to happen here): <!-- r4 (cold-Opus, conf 74, one concrete
   three-worker timeline, treated as confirmed at the round-2/3 evidence bar): round-3 taught
   "revoke by the ID already in hand, not by a name lookup" — but applied that lesson only to a
   fenced-out worker's own path, not to THIS branch, which still revoked "that orphan token"
   (singular, found by name) and then overwrote any already-recorded `provider_token_id` without
   ever revoking the ID it was overwriting. Because CF permits duplicate names (this spec's own
   premise, § transport) and the deadletter path (below) deliberately leaves a token live under
   this exact deterministic name whenever a fenced worker's by-ID revoke itself fails, a later
   reclaim's name lookup can return TWO live tokens: the row's own already-recorded one and a
   deadletter'd orphan. Revoking "that orphan" (whichever the lookup happens to return first) and
   overwriting `provider_token_id` can leave the OTHER live token — the one no longer referenced
   by anything — unrecorded and unrevoked: an unrecorded, unrevoked live orphan, the exact P0
   class rounds 1 and 3 both exist to prevent. Fixed by making revocation symmetric (revoke a
   known recorded ID directly, never by lookup) and exhaustive (handle the lookup returning more
   than one live token, since CF's own duplicate-name permission makes that a real case, not a
   hypothetical): --> **first, if this row's `provider_token_id` is already set (non-null),
   revoke that specific recorded ID directly** (by ID, not by name lookup — symmetric with the
   fenced-out-worker path above); **then perform the name lookup**, and if it returns any live
   token(s) under the deterministic name (zero, one, or — because CF permits duplicate names —
   more than one; do not assume at most one), **revoke every one of them**. <!-- r5 (agy-print
   P1, cold-Opus P2 — both independently flagged the same ambiguity, disagreeing only on
   severity; fixed regardless of which rating is right, since it's cheap and this is the
   credential-minting hot path): the previous wording — "only once all revocations above have
   either succeeded OR the row's OWN previously-recorded token has been confirmed revoked do
   proceed to mint" — read literally as `(all succeeded) OR (own token revoked)`, which would
   permit proceeding to mint even if a *name-lookup* orphan's revoke failed, as long as the row's
   own recorded token specifically was revoked. That's backwards from the intent (the row's own
   token is one of the revocations required, not an alternative to the rest of them) and, worded
   that way, is exactly the shape of bug that leaks an orphan. The governing sentence right after
   it ("if any required revocation fails... fail closed") and the regression test already pin the
   correct behavior, so this was cleanup rather than a shipped-bug gap — removing the ambiguous
   "or" clause entirely closes even the reading risk. --> **Every revocation identified above —
   the row's own previously-recorded token (if any) AND every token the name lookup returned —
   must succeed (idempotent: "already revoked / not found" counts as success, see below) before
   proceeding to mint a fresh token** with the same deterministic name, capturing its new secret
   and immediately recording the new `provider_token_id`. If **any** of those required
   revocations fails for a reason other than "already gone" (e.g. the restricted token lacks
   delete permission, or the CF API call errors) — <!-- r4: "already revoked / not found" from CF
   is NOT a failure here, it's the success case (idempotent revoke); see the sweep's own
   404-handling rule, below, which applies uniformly to every revoke call site in this spec, not
   just the sweep's. --> the row must **fail closed** — mark `failed` with a reason that names
   every irrecoverable orphan token ID involved, not silently retry or pretend success — <!-- r5
   (cold-Opus Finding B): additionally, INSERT each un-revoked orphan token ID into
   `arb_messages_deadletter` (`ON CONFLICT (provider_token_id) DO NOTHING`, so re-inserting an
   already-tracked one from the fenced-out-worker path is a harmless no-op) rather than only
   naming it in the row's free-text failure reason. Round-3's fenced-out-worker path already does
   this; this branch didn't, which meant an orphan discovered here (as opposed to via the fenced
   path) was tracked only in prose, invisible to the automated sweep, and dependent entirely on an
   operator reading the failure reason. Deadlettering it here restores the same auto-swept
   backstop the fenced-out-worker path already gets, rather than leaving this path's orphans as a
   strictly-operator-required residual when the mechanism can track them itself. --> and surface
   as an operator-required residual regardless (the deadletter entry gets it revoked
   automatically in the common case; the failure reason on the row is still the human-readable
   trail of what happened and why). This makes a lease-reclaimed retry converge on exactly one
   live, deliverable token — never leaving a second, unrecorded one behind — rather than either
   accumulating orphans or fabricating a result from data that no longer exists.
9. **Executor holds a restricted minting token, not the global CF API key** — defense in depth
   even if the executor process itself is compromised. <!-- r1 (cold-Opus P1-2), r2 (agy-print,
   cold-Opus P1-3 — same finding independently confirmed): the restricted token needs THREE
   permissions for this design to work, not one — **create** (obviously), **delete/revoke**
   (invariant 7's revocation story and invariant 8's revoke-then-remint path both depend on it),
   and **list/read by name** (invariant 8's lookup-before-create step depends on it — without it,
   the executor can't tell whether a prior attempt already minted, and would either double-mint
   or block forever). All three, plus the sub-day-TTL question, are must-verify-live items, not
   assumptions — see § Cloudflare capability facts' live-verification gate, below, updated
   accordingly. r3 (agy-print — not independently corroborated by the other three round-3
   reviewers, but specific and well-evidenced enough to fold in rather than wait for a fourth
   round to re-ask the same question): round-1/2's framing ("a token whose only power is
   creating, listing, and revoking tokens *it created*") assumed Cloudflare's permission model
   offers a scope narrower than "manage all API tokens on the account." agy-print's read of
   Cloudflare's actual permission groups is that token management (the permission group covering
   list/revoke) is not scoped to tokens the managing token itself created — a token holding that
   permission group can list and revoke *any* API token on the account, including ones
   unrelated to this plane. If that's accurate (this is exactly the kind of provider-capability
   claim this spec has repeatedly gotten wrong on first pass and repeatedly required live
   verification to pin down — see the sub-day-TTL question, which has the same shape), the
   blast radius of a compromised executor is the account's entire token inventory, not just
   ARB-Messages-minted tokens, which is a materially different security story than "restricted
   to what it created" implies. Don't take this on faith either way — added as an explicit
   fourth live-verification item below, alongside the three-permissions check, specifically:
   "does the token-management permission group scope to tokens the holder created, or to all
   account tokens?" If it's account-wide (no narrower scope exists), the mitigation is
   operational, not a permission-scoping fix: run the executor against a **dedicated Cloudflare
   user/account** holding only the tokens this plane manages, so "all tokens on the account"
   is a small, known set rather than the operator's entire Cloudflare token inventory — this
   becomes a required plan task if the live-verification confirms the narrower scope doesn't
   exist. -->

**New — token revocation sweep.** <!-- r1 (agy-print Finding 6): round-0 said the executor "can
revoke on job failure or lease expiry" (old invariant 7 wording) but named no process that
actually does it. A `failed` or lease-expired row with a recorded `provider_token_id` (invariant
7 guarantees one exists if a mint ever succeeded) needs to actually get revoked, or it
accumulates as a live stale credential forever.
r2 (agy-print Finding 2, pi-GLM F2, cold-Opus P2-1 — 3-way convergence): the round-1 sweep only
covers `failed`/lease-expired rows, but a `done` row whose `delivered_at` got set without the
agent actually receiving the body (a dropped inline-wait response, or — before the P1-1 fix
below — a wrong-actor poll) is a live token with no failure status to trigger the sweep at all.
--> `executor.py` runs a periodic sweep
(alongside the claim loop, same process is fine for this pass's scale) that revokes and marks
`token_revoked_at` (a new nullable timestamp column, parallel to `delivered_at` — revocation is
a property of the associated CF token, not a new request-lifecycle status, so the `status` enum
stays `pending/claimed/done/failed/denied` unchanged) for **two** categories of row with a
non-null `provider_token_id` and a null `token_revoked_at`:
1. `failed`/lease-expired rows (the original round-1 case: a request that legitimately fails
   *after* a successful mint, e.g. the seal step throws).
2. <!-- r2 (agy-print Finding 2, pi-GLM F2, cold-Opus P2-1): --> `done` rows where `delivered_at`
   is either still null past a grace window (`delivered_grace_seconds`, a new `Settings` field,
   default e.g. 3600s — a mint that was never picked up at all) **or** old enough that a
   legitimately-receiving agent would already have consumed-and-destroyed it per invariant 6
   (same grace window, measured from `delivered_at` this time) — either way, past the grace
   window there is no further legitimate reason for the token to still be live, whether or not
   delivery actually reached the agent. This is what closes the P1-1 wrong-actor-poll orphan and
   the dropped-inline-response orphan: both leave a `done`+`delivered_at`-set row whose real
   owner never got a usable token, and this sweep condition reaches them without needing to know
   *why* delivery didn't succeed.

<!-- r4 (codex P1, agy-print P1, cold-Opus P1 — 3-way convergence): round-3 asserted the sweep
"also processes" arb_messages_deadletter but the two categories above are both arb_messages-only
and neither one's selection criteria (status/delivered_at) apply to that table at all — a
literal implementation touches the deadletter table zero times. This third category makes the
claim real, mirroring category 1/2's shape (select unrevoked, revoke, mark revoked). -->
3. **`arb_messages_deadletter` rows where `token_revoked_at` is null** — revoke each recorded
   `provider_token_id` by ID, then set `token_revoked_at` (never delete the row — it's a
   permanent record that a leak happened and was cleaned up, per the table's own definition
   above). This is the actual drain for the round-3 fenced-out-worker backstop and the round-4
   revoke-then-remint fix (§ Security invariant 8) — both write to this table on a revoke
   failure; without this category, those entries would sit forever. <!-- r5 (agy-print P1): -->
   Every attempt (success or failure) increments `attempts` and sets `last_attempt_at`
   (§ Live code location's `arb_messages_deadletter` definition) — this is the observability
   signal that distinguishes a fresh entry the next sweep pass will likely clear from a
   persistently-failing one an operator needs to look at, without introducing a hard retry
   ceiling that could give up on a transient failure permanently.

<!-- r4 (agy-print P2, cold-Opus P2 — 2-way convergence): applies uniformly to every revoke call
site this spec describes (the found-branch revocations above, a fenced-out worker's own revoke,
and all three sweep categories here — the third, deadletter, category didn't exist at r4 draft
time; r5 (cold-Opus Finding C): "both" corrected to "all three" now that it does), not just this
paragraph. -->
**A revoke call that returns "already revoked / token not found" from Cloudflare is treated as
success everywhere**, not a failure — revocation is idempotent by nature (the end state, "this
token no longer works," is what matters, and a 404 means that state already holds). Without this
rule stated once and applied everywhere, a second concurrent revoke of the same token (see the
sweep-concurrency point immediately below) or a retry after an already-successful revoke would be
misclassified as a failure and could wrongly trigger a fail-closed/deadletter path for a token
that's actually already gone.

<!-- r4 (agy-print P2, cold-Opus P2 — 2-way convergence): the sweep has no analogue of the main
claim loop's SKIP LOCKED, and every executor process runs its own sweep loop (this pass's
scale assumption, stated above) — so two sweeps can select and act on the same row concurrently.
-->
**Both sweep categories (and the deadletter category above) select with
`SELECT ... FOR UPDATE SKIP LOCKED`**, matching the main claim loop's concurrency discipline
(§ Transport) — this, combined with the idempotent-revoke rule just above, means two concurrent
sweep processes acting on the same orphan is at worst a harmless double revoke-attempt (one
succeeds, the other's redundant call 404s and is treated as success), never a correctness issue.

<!-- r3 (agy-print): the sweep must survive a pause, or pausing the plane during an incident
(exactly when an operator most wants existing live tokens revoked) also stops revocation. -->
**The sweep runs as its own loop, independent of both kill switches** — it checks neither
`Settings.messages_enabled` nor the `arb_messages_settings` pause flag (§ Containment
executor-side step 4). Pausing or disabling the plane stops new *claims* (new mints); it must
never stop the sweep from continuing to revoke already-minted, no-longer-needed tokens — an
operator who pauses the plane during a suspected-compromise incident wants exactly the opposite
of the sweep also going quiet.

<!-- r4 (codex, pi-GLM, cold-Opus — the triple-failure residual all three independently raised
in some form): the brief specifically asked whether a deadletter-INSERT-itself-failing has a
true no-recovery-path residual. Answer: yes, and it's acceptable, but it must be discoverable. -->
**If a fenced-out worker's revoke fails AND the subsequent `arb_messages_deadletter` INSERT also
fails** (e.g. the DB connection itself is down at that moment), the worker has no remaining
durable channel — this is a genuine, irreducible triple-failure residual (fence-loss + revoke-
failure + insert-failure, three independent low-probability events) that no design can fully
close, since the process may be killed between the external side effect and any attempt to
record it. It is bounded and acceptable: the token is TTL-limited (invariant 4) and will expire
on its own. It must not be silent, though — the worker emits a final `CRITICAL`/`ERROR`-level
log line carrying the orphaned `provider_token_id` as the last-resort discoverability path, so
an operator reviewing logs after an incident (or a log-scraping alert on that log level) can
still find and manually revoke it via the restricted token's own list permission (§ Cloudflare
capability facts) before the TTL would otherwise close it out.

## Cloudflare capability facts (from the original artefact — re-verify before implementing, not
before this spec's review)

- CF exposes a token-creation API (user- and account-owned tokens); permission groups scope to
  specific resources (zone-level, not just account-wide) — a minted token can be bound to one
  zone.
- TTL via `not_before`/`expires_on`.
- **Open, must be confirmed by a live test mint before this pass is considered done, not assumed
  from the design artefact:** whether `expires_on` honors sub-day (e.g. 5-minute) TTLs, or is
  day-granular like the dashboard. This gates whether "shortest TTL" (invariant 4) is actually
  achievable at the granularity the design assumes.
- <!-- r1 (cold-Opus P1-2), r2 (agy-print, cold-Opus P1-3 — expanded from two permissions to
  three): --> **Open, must also be confirmed by the same live-verification gate:** the restricted
  minting token (invariant 9) needs **three** permissions verified independently, not assumed
  bundled together as "restricted = safe subset": token-**create** (the baseline), token-
  **delete/revoke** (invariant 7's revoke-on-failure story and invariant 8's revoke-then-remint
  path both depend on it), and token-**list/read by name** (invariant 8's lookup-before-create
  step depends on it — without it, the executor can't tell whether a prior attempt already
  minted under this row's deterministic name, and would either double-mint or have to block
  forever waiting for something it can't observe). If any of the three is missing, invariant 8's
  mechanism is inoperable in a different way for each: no create → nothing works; no
  delete/revoke → orphans from the crash-then-restart case can never be cleaned up, only
  detected; no list/read → the executor can't even detect an orphan to begin with. Also verify:
  is a token **name settable at create and queryable by exact name afterward** (not just by ID)?
  The whole lookup-before-create mechanism assumes name is a stable, queryable identifier — this
  is a load-bearing assumption about Cloudflare's API shape, not just a permission question, and
  isn't safe to take on faith from the design artefact either. If the restricted token can't
  practically carry all three permissions (e.g. Cloudflare's permission groups don't offer a
  create+delete+list-but-nothing-else bundle), the design needs a different answer (e.g. two
  separate restricted tokens, one mint-capable and one list+revoke-capable) before this pass can
  be considered done.
- <!-- r3 (agy-print): a fourth verification item, distinct from "does the token have these
  permissions" — "what do these permissions actually reach." --> **Open, must also be confirmed
  by the same live-verification gate:** does Cloudflare's token-management permission group
  (the one granting list/revoke, invariant 9) scope to *tokens the holder itself created*, or to
  *all API tokens on the account*? If the invariant-9 framing ("a token whose only power is
  managing tokens it created") turns out not to correspond to an actual Cloudflare permission
  scope — i.e. if list/revoke permission is inherently account-wide — the blast radius of a
  compromised executor is the account's entire token inventory, not just this plane's tokens,
  which is a materially different security story than the design assumes. If confirmed
  account-wide with no narrower scope available, the plan must add running the executor against
  a **dedicated Cloudflare user/account** holding only the tokens this plane manages, as the
  operational mitigation for what permission scoping alone can't provide.

## Testing

Follow `tests/arb_email/`'s established pattern exactly: flat `tests/arb_messages/` directory,
fake collaborators over real ones (`FakePostgres`/`FakeClient` matching `arb_email`'s
`FakeClient`, an audit sink as a plain list-appending callable for assertion), injected
`now`/`wall_now` callables for deterministic lease/TTL testing (matching
`test_rate_minute_sliding_window_recovers`'s pattern), explicit deny-path test naming (e.g.
`test_out_of_allowlist_zone_denies_and_audits_without_calling_cf`, mirroring `arb_email`'s
`test_rejects_emit_denial_audit_and_never_call_client`).

Minimum required tests (the plan will expand this into concrete task-level detail).
<!-- r1: significantly expanded — all four reviewers independently flagged the round-0 list as
too shallow for a credential-minting component (codex's testing-plan-sufficiency point, pi-GLM
§5, agy-print's action-plan item 7 implicitly, cold-Opus P1-5 explicitly listing the same gaps).
The additions below are the direct regression guards for every P0/P1 fixed in this revision. -->

- `test_run.py`: `setup_schema` is idempotent (runs twice without error), creates both tables
  with the right columns/constraints, including the `(status, claimed_at)` compound index and
  the `arb_agent_keys (agent_id) WHERE revoked_at IS NULL` partial unique index. **New (r2,
  agy-print P2/F-04):** `arb_messages_settings` is created with a `paused` row defaulting to
  unset/false, seeded via `ON CONFLICT DO NOTHING` (re-running `setup_schema` a second time with
  the row already set to `'1'` must leave it at `'1'`, not reset it — the direct regression
  guard for the r4 cold-Opus P2 fix). **New (r3, codex/agy-print, r4 schema update):**
  `arb_messages_deadletter` is created with the right columns, including the r4-added
  `token_revoked_at`.
- `test_store.py`: enqueue → claim (SKIP LOCKED semantics — two concurrent claimers never claim
  the same row; use a real Postgres test fixture or a careful serialized-transaction fake, not a
  naive in-memory dict that can't actually exercise row-locking) → ack → result-write; lease
  expiry makes a claimed-not-acked row reclaimable; dedupe on the `UNIQUE (agent_id, request_id)`
  compound key <!-- r2 (cold-Opus P1-1): compound, not the round-1 bare `request_id` --> rejects
  a duplicate enqueue from the *same* agent but allows the *same* `request_id` string from two
  different agents (the direct regression guard for the cross-agent-collision fix). **New:** a
  second poll of an already-`delivered_at` row returns `already_delivered`, not the body again
  (invariant 5). **New (r2, pi-GLM F1):** the fencing guard — a result-write conditioned on a
  stale `claimed_at` (simulating a hung worker whose row was reclaimed) affects zero rows and
  the caller can detect this and abort, rather than silently succeeding. **New (r4, agy-print
  P1 — the direct guard for the premature-delivery bug):** `read_and_mark_delivered` on a
  `pending` row returns `{"status": "pending"}` and leaves `delivered_at` null; same for a
  `claimed` row; only a call against a genuinely `done` row with a sealed body sets
  `delivered_at` and returns it. A poll sequence (poll while `pending` → executor completes →
  poll again) must succeed on the *second* poll, proving the first poll didn't consume the
  one-time slot — this is the end-to-end regression guard for the exact failure trace agy-print
  described (an early legitimate poll permanently losing a token that was never delivered once).
- `test_keys.py`: self-registration writes `agent_id` from the passed-in actor, never from a
  caller-supplied field (there should be no code path to even attempt passing one — assert the
  function signature has no such parameter, not just that a value is ignored); a second
  registration attempt for an agent that already has a live key is rejected by the partial
  unique index, not silently accepted as an overwrite; a revoked key is excluded from "live key"
  checks; re-registration succeeds once the prior key is revoked (first-write-wins-per-epoch).
  **New (r2, agy-print P2/F-05):** a malformed `pubkey` (wrong length, non-decodable) is rejected
  at registration with a clear error, before any `INSERT` — the direct regression guard for the
  lock-in gap. **New (r2, agy-print P2/F-06):** an end-to-end crypto round-trip test — register a
  real PyNaCl-generated keypair's public half, `seal()` a known plaintext to it, `unseal()` with
  the matching private key (held only in the test, never by server code), assert the round-trip
  recovers the original plaintext. This is the only test that would catch a key-format or
  library-usage bug the unit-level mocked tests can't see.
- `test_mint_cloudflare.py`: a successful mint (against a fake CF client) writes
  `provider_token_id` **before** the sealed body/`done` status (assert the write order, e.g. via
  a fake client that raises after returning the token but before the caller's next statement,
  and confirm `provider_token_id` already landed); a CF API error is recorded as `failed`, not
  silently swallowed. **New (codex P1, structural-sealing guard):** `store.py`'s
  `write_sealed_result` has no parameter or code path that accepts a plaintext token — assert
  this at the type/signature level (no `token`/`plaintext` argument exists) and add one negative
  test that constructs a raw token string and asserts there is no way to reach a `done` mint row
  in the test's own fixture without it having passed through `seal()` first (e.g. grep the
  written body for the known plaintext value and assert it's never present in any envelope
  column or an unsealed body). **Rewritten (r2, codex + agy-print — replaces the round-1
  "adopt and complete" test, which encoded a mechanism that cannot work): the direct guard for
  invariant 8** — simulate "CF mint succeeds, then the process is interrupted before the DB
  write, then a retry (lease-reclaim) occurs against a fake CF client that (a) returns a secret
  only from its create call, never from list/get, matching real Cloudflare behavior, and (b) has
  a pre-seeded orphan token under the deterministic name from the 'first attempt'" and assert:
  (a) the retry's lookup-before-create finds the existing orphan, (b) the retry revokes it via
  the fake client's revoke call, (c) the retry mints a *fresh* token and seals *that* token's
  secret (not the orphan's, which the fake client would refuse to disclose — assert the test
  fails if the implementation tries to seal a value the fake never returned from create), (d)
  exactly one live token exists at the end (the fake client tracks its own revoked-vs-live set).
  **New (r2):** if the fake client's revoke call itself fails, the row ends `failed` with a
  reason naming the orphan — not a silent retry loop and not a false `done`. **New (r3, codex
  P0/agy-print/cold-Opus — the direct guard for the round-3 P0):** simulate "worker A claims,
  its CF create call is still in flight when its lease expires; worker B reclaims, looks up
  (finds nothing, since A's create hasn't landed yet), mints its own token, completes normally;
  THEN A's create call returns" and assert (a) A's fenced `provider_token_id` write affects zero
  rows (the existing fencing-guard test covers this part), (b) A **revokes the token it just
  received** using the ID from its own create response — not a lookup, since B's completed row
  means a lookup-before-revoke would find B's token instead — before stopping, (c) if that revoke
  call itself fails, A's orphaned token ID is recorded in the deadletter table (§ Live code
  location), not silently dropped, (d) exactly one live token (B's) exists at the end. **New
  (r3, pi-GLM F1/cold-Opus — the direct guard for the invariant-8 wording fix):** simulate a
  crash landing exactly between the `provider_token_id` write and the seal step (row has
  `provider_token_id` set, body null, not `done`) and assert a retry's lookup-before-create still
  fires revoke-then-remint (not a false "provider_token_id already set, nothing to do" no-op).
  **New:** the sweep function revokes a `failed` row's `provider_token_id` and sets
  `token_revoked_at`, and is a no-op (doesn't double-revoke) on a row it already swept. **New
  (r2, agy-print F2/pi-GLM F2/cold-Opus P2-1):** the sweep also revokes a `done` row whose
  `delivered_at` is null past `delivered_grace_seconds`, and a `done` row whose `delivered_at` is
  set but older than the same grace window — both without needing to know why delivery didn't
  reach the agent. **New (r4, cold-Opus P0 — the direct guard for the round-4 P0):** the full
  scenario is a genuine three-worker timeline, reachable because BOTH reclaiming workers stall,
  not just the first — worker A hangs inside its CF create past its lease; worker B reclaims,
  finds nothing (A's create still in flight), mints, fence-writes `provider_token_id=B`, and then
  *also* stalls before sealing, past its own lease (so the row is still not `done`); A's create
  then lands, A's fenced write fails, A revokes its own token by ID but that revoke itself fails,
  A inserts into the deadletter table (still live, name N); B's lease then also expires; worker C
  reclaims the still-not-`done` row, and C's deterministic-name lookup returns **both** live
  tokens (A's deadletter'd one and B's recorded one) under name N. The test constructs this
  directly (a row with `provider_token_id` already set to a known "B" token ID, not `done`, plus
  a fake CF client seeded with two live tokens under the deterministic name — B's recorded one
  and a second standing in for A's deadletter'd orphan) rather than simulating the full stall
  timing, since the reachable *state* at the point revoke-then-remint runs is what the mechanism
  actually has to handle, regardless of how many workers or stalls produced it — and asserts the
  revoke-then-remint path revokes **both** (the row's own recorded ID directly, and every token
  the name lookup returns) before minting fresh, with the row's final `provider_token_id`
  corresponding to a token that is provably the only one left live at the end (no token from
  either revoked set remains unaccounted for). **New (r4, cold-Opus, cross-check):** a plain two-worker version of the
  round-3 test (already exists, see above) still passes unmodified — the P0 fix must not require
  behavior different from round-3's fix in the simpler case where the name lookup returns exactly
  one token and `provider_token_id` was not already set. **New (r4, codex P1/agy-print P1/
  cold-Opus P1):** `test_run.py`/`test_mint_cloudflare.py` — the deadletter sweep category
  actually revokes a deadletter row's `provider_token_id` and sets its `token_revoked_at` (never
  deletes the row); a deadletter row already marked `token_revoked_at` is skipped, not
  re-revoked; a CF revoke call that returns "already revoked / not found" is treated as success
  everywhere this mechanism calls revoke (found-branch revocations, the fenced-out-worker path,
  and all three sweep categories) — one test per call site asserting a 404-shaped fake response
  doesn't trigger the fail-closed path. **New (r4, agy-print P2/cold-Opus P2):** two concurrent
  sweep passes over the same orphan (`SELECT ... FOR UPDATE SKIP LOCKED` semantics, matching the
  main claim loop) — the second pass's redundant revoke attempt doesn't error or double-process.
  **New (r5, agy-print P1 — the direct guard for the deadletter retry-tracking gap):** a
  deadletter row whose revoke fails on a fake client seeded to always error has `attempts`
  incremented and `last_attempt_at` updated on each sweep pass, `token_revoked_at` stays null,
  and the row is never deleted — running the sweep three times against a permanently-failing
  entry leaves `attempts=3`, still present, still selected next pass (proving there's no silent
  give-up, matching the spec's "no hard cutoff" design). **New (r5, cold-Opus Finding B):** the
  found-branch's fail-closed path (when a required revocation fails) inserts the un-revoked
  orphan into `arb_messages_deadletter` (`ON CONFLICT (provider_token_id) DO NOTHING`), and a
  subsequent sweep pass picks it up and revokes it via the deadletter category — the direct
  regression guard proving this path's orphans are auto-swept, not just named in a failure
  reason string.
- `test_door_tools.py`: door-side pre-checks (§ Containment, door-side steps) — scope denial,
  allowlist-pre-check denial, missing-key-pre-check denial — each asserting the audit sink fired
  and (for denials) that no row was ever enqueued as `pending`. **New:** assert `agent_id` on
  the enqueued row always equals the authenticated actor regardless of any other input, since
  there is no parameter through which a caller could influence it (this is the direct regression
  guard for the P0-1/P0-2 fixes — the test should fail loudly if a future edit ever adds an
  `agent_id` parameter to the door tools). **New (r2, agy-print/cold-Opus P1-2):** an access
  token with a `None`/absent `client_id` is rejected (raises/denies) by `_actor()`, never falls
  through to a shared `"mcp"` identity — the direct regression guard against inheriting
  `arb_email`'s fallback. **New (r2, pi-GLM F3):** a `zone` not in `Settings.allowed_zones` is
  denied at the door pre-check, matching the existing allowlist/key pre-check pattern. **New
  (r2, codex/agy-print/cold-Opus P1-1):** `messages_poll` for a `request_id` that exists but
  belongs to a different `agent_id` returns "not found," does not set `delivered_at`, and the
  audit sink records the mismatch; the legitimate owner's subsequent poll still succeeds
  normally (proving the wrong-actor poll didn't consume the delivery slot).
- `test_executor.py`: <!-- r1: new file, since the executor-side checks are now a distinct
  component per the door/executor split. --> the executor's *independent* re-validation —
  construct a claimed row whose stored `policy_decision` column says "approved" but whose
  `agent_id`/zone is **not** in the executor's own loaded `Settings.allowed_agents`/
  `allowed_zones`, and assert the executor denies and never calls the CF client (the direct
  regression guard for P0-1: an executor that trusted the row's own claim would wrongly proceed
  here). Also: key-liveness re-check at claim time using a key that was live at enqueue-fixture
  time but is revoked before the executor's claim (TOCTOU guard); kill-switch re-check using the
  executor's own env distinct from the door's, **and (r2) against the new
  `arb_messages_settings` pause flag**; the CF-policy-construction step never accepts a
  row-supplied policy blob (assert only the small capability-vocabulary path is reachable).
  Explicitly test that the executor's code path never calls
  `mcp.server.auth.middleware.auth_context.get_access_token()` or anything requiring an MCP
  request context — it must be runnable as a bare script against a Postgres connection alone.
  **New (r5, agy-print P1 — the direct guard for the missing-retry-tracking gap):** a row whose
  `attempts` already equals `Settings.max_retries` is claimed one more time (the claim itself
  still succeeds and increments `attempts`), but the executor marks it `failed` with a
  "max retries exceeded" reason instead of attempting to mint, and never calls the CF client;
  a row below the limit proceeds normally. A simulated repeatedly-crashing claim (fixture that
  always raises) reclaimed `max_retries + 1` times ends in `failed`, not an infinite loop —
  the end-to-end regression guard for the poison-pill scenario agy-print described.
- `test_door_wiring.py`: `register_messages_tools` returns `False` fast on missing required env
  or kill-switch off, without raising; returns `True` and registers the tool on valid config.
  **New (codex P1):** `messages.request` is present in both `valid_scopes` and `default_scopes`
  in `src/arb_memory/mcp/server.py`, matching `tests/arb_email/test_door_wiring.py:59-77`'s
  scope-wiring regression test pattern. **New (r2, codex):** all three tools
  (`messages_request`, `messages_register_key`, `messages_poll`) are registered on valid config
  — the direct regression guard for round-1 having described `messages_poll` without ever
  registering it.

**Live verification (this pass's own gate, distinct from unit tests):** <!-- r5 (codex P2): this
summary paragraph had drifted from the fuller checklist § Cloudflare capability facts actually
requires (list/read-by-name, name settable/queryable, token-management blast radius) — not a
design gap, just a stale summary that could mislead a plan-writer skimming this paragraph alone
into under-scoping the live check. Expanded to match. --> one real mint against Cloudflare's
actual API (not a fake), confirming (a) the minted token is genuinely scoped to the requested
zone only, (b) the sub-day TTL question (§ Cloudflare capability facts), (c) the token can be
revoked via `provider_token_id`, (d) the restricted minting token can **list/read** tokens by
exact name (invariant 8's lookup-before-create depends on it), (e) a token's name is genuinely
settable at create and queryable by that exact name afterward, and (f) whether the
token-management permission scopes to tokens the holder created or to the whole account (§
Cloudflare capability facts' fourth item) — if account-wide, the dedicated-CF-account mitigation
becomes a required plan task, not optional. This needs the real `ARB_MESSAGES_CF_MINTING_TOKEN`
— a live secret, not something a unit test fixture can stand in for.

## Risks / open questions

1. **Sub-day CF TTL is unconfirmed** — see § Cloudflare capability facts. If `expires_on` turns
   out to be day-granular only, invariant 4 ("shortest TTL the provider allows") is weaker than
   the design assumes; not a blocker for this pass (day-granular is still much better than
   "standing credential"), but worth knowing before promising tighter TTLs downstream.
2. **`SELECT ... FOR UPDATE SKIP LOCKED` testing needs a real Postgres connection**,
   <!-- r1: resolved operationally, not just noted — a throwaway, disposable local Postgres
   container (`arb-messages-test-pg`, `postgres:16` via Docker, port 5599, credentials
   `arb_messages_test`/a local throwaway password) has been stood up specifically for this
   pass's testing, kept deliberately separate from the shared `arb-memory-pg-dev` dev database
   so this experimental schema never touches it. Connectivity verified via `psycopg` 3
   (`.venv/bin/python`, already installed). The plan/implementation dispatch should be given this
   DSN directly (not committed to the repo — it's a disposable local secret) rather than
   depending on DO-hosted or shared dev DB credentials the implementor may not have. --> not a
   pure in-memory fake, to actually exercise row-locking semantics (a naive dict-based fake can't
   prove two concurrent claimers don't double-claim).
3. **DO/`proxy` are explicitly out of scope** for this pass (§ Scope) — don't let plan review or
   implementation scope-creep into them; a future spec handles that expansion on the
   already-generic rails this pass builds.
4. **E2E/live-provider testing may be blocked by operator permissions** — <!-- r1: the SKIP
   LOCKED / general Postgres testing part of this risk is resolved by item 2 above. What
   remains genuinely blocked without operator action: the **live Cloudflare mint verification**
   (needs the real `ARB_MESSAGES_CF_MINTING_TOKEN`, a live secret that must actually exist in
   Cloudflare first, including the sub-day-TTL and revoke-permission checks this revision added
   to the live-verification gate) and provisioning the door/executor's actual **restricted DB
   roles** against the real Postgres instance this plane will run against in deployment (the
   role-restriction requirement added in § Live code location, above). If genuinely blocked at
   implementation time, these should surface as a named, explicit residual — not a
   silently-skipped step. -->
5. **Key rotation has no self-service path this pass** (§ `arb_agent_keys`) — a deliberate scope
   cut, not an oversight; noted here so it isn't mistaken for a gap the plan forgot to address.
   Revisit if rotation friction becomes a real operational problem.
