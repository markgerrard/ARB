# Bus-side gate: make unverified findings undispatchable

**Status:** design agreed, not implemented.
**Derived from:** ARB Memory `art-6130c902e461a3fb` v1 — *red-before-remediate* (Mark, 2026-07-26).
**Authority trail:** §§1–6 scoped from the design note. §7 (verification identity), the
lane-keying decision in §8, and the two provenance additions in §7.3 were **added at review**
on 2026-07-26, not carried from the design note — recorded here so the arc-closure sweep sees
the crossing rather than inferring a lineage the thread doesn't support. All MUST-strength
rules below are co-signed by Mark, 2026-07-26.
**v3** closes three findings raised by Mark's review of v2 — F1 (cross-family MUST had no data
path), F2 (verifier re-run was self-report), F3 (exempt-lane membership unresolved at the gate) —
plus CHECK constraints on the enum columns the views trust. F1 and F2 were the spec asserting
properties its own schema could not carry: the same "assertion outruns mechanism" defect the
document is about, found by applying the document to itself.
**v4** is a fidelity fix, no design change: v3's stored copy had lost the GRANT trust-story
paragraph, the local copy had lost the `## 6` heading to a bad edit anchor, and both still said
`arb_gate_reader` holds *two* views after F3 added a third. Recorded rather than silently
corrected — a spec that argues for approve-by-hash has to show its own version defects.
**v5** adds the brief `assumptions` section (§5.2) — readiness declarations as a third claim class —
and records the doc lint (`src/arb_memory/doc_lint.py`) that now mechanically checks this
document's structure and count claims, seeded from the v3/v4 defects above.
**v6** closes F4 (a `NOT NULL` re-run reference proves a pointer exists, not that the artefact it
cites was harness-authored — self-report moved one pointer deeper) and aligns
`decorrelation_provenance`'s filter with `attested`'s, which had been measuring a different
population.

---

## 1. What this is, and what it is not

`docs/evidence-first-remediation.md:9` already states the rule: *"No remediation task … may be
created from an observation alone."* That document is 69 lines of correct doctrine. The design
note's evidence is that it **did not fire** — along with two mid-session memory entries and a
raised reasoning-effort setting.

This spec adds no doctrine. It gives existing doctrine its first enforcement surface.

The distinction matters for scope: nothing here needs to argue that evidence-first is right. The
only question is where the rule gets teeth, and the answer is that it must be a pipeline property,
because acceptance error rates are unmeasurable from inside the seat that would be measuring them.

## 2. The gate chain

```
finding → red artefact → remediation dispatch → green artefact → close-time reconcile → merge
```

**Doctrine (MUST):** remediation follows evidence, not review. No implementation is dispatched
against a claim that has not been demonstrated in front of the harness.

"Demonstrated" rather than "failed": the observation class — `grep -c '409' → 0` — is a legitimate
reproduction that never goes red.

## 3. Architecture: two lanes, one lifecycle

```
finding (unconfirmed)
   │
   ├── EXEMPT LANE ─────────────────────────────────┐
   │   dispatch admitted with NO claim ref          │
   │   worktree: no push rights to shared remote    │
   │   executes freely; builds + runs the probe     │
   │   deliverable → probe PACKAGE artefact         │
   │      (source + fixtures + run log)             │
   │      written under harness identity            │
   │   lease released → worktree gone               │
   └────────────────┬───────────────────────────────┘
                    │ red artefact + attestation ⇒ claim admissible
                    ▼
   ┌── GATED LANE ──────────────────────────────────┐
   │   dispatch MUST carry claim_ref                │
   │   bridge resolves ref → admissible-as-of-now   │
   │   implementer rehydrates probe from artefact   │
   │   red → green on that exact probe              │
   │   probe re-lands IN the remediation diff       │
   │      as a permanent regression test            │
   └────────────────┬───────────────────────────────┘
                    ▼
        close reconcile re-resolves everything → merge
```

**Default-deny (MUST).** Every `request` envelope to a posture-bearing seat requires an admissible
claim ref unless it arrives on the exempt lane. Omission blocks; it does not pass.

**Lanes are defined by what their outputs can reach, never by what the dispatch says it is for.**
A lane whose membership is decided at dispatch time just moves the classification judgment one
field over. An intent field may exist for *routing*, harmlessly: misdeclaration is self-punishing
rather than gate-evading, because declaring remediation as investigation returns analysis that
cannot become a mergeable diff. Lying is made useless rather than detectable — the cheaper and
stronger property.

**The exempt lane is the gate's front door, not a hole in it.** Red-before-remediate requires that
someone be dispatchable to *build* the red probe before any claim ref exists; repro-construction is
by definition a dispatch about an unconfirmed finding. That is the lane's primary legitimate
traffic, and it fits the capability definition exactly — its deliverable is an artefact stored
under the harness identity, not a diff.

## 4. Data model

New in `src/arb_memory/schema.sql`. Sits *alongside* `artefacts` (`schema.sql:3-15`) rather than
extending it: artefacts stay content-addressed and statusless; claims carry the lifecycle.

```sql
CREATE TABLE IF NOT EXISTS claims (
    claim_id     text PRIMARY KEY,
    finding_ref  text NOT NULL,
    status       text NOT NULL DEFAULT 'unconfirmed'
                 CHECK (status IN ('unconfirmed','confirmed','retracted')),
    severity     text,                                  -- ORDERING ONLY (see §8)
    -- Author identity (F1): the cross-family MUST in §7.3 is uncheckable without it.
    author_seat   text NOT NULL,
    author_family text NOT NULL,
    author_family_provenance text NOT NULL
                 CHECK (author_family_provenance IN ('wire','configured')),
    probe_artefact_id text,
    probe_artefact_version int,
    review_by    timestamptz,                           -- accepted-risk expiry; NULL = none
    confirmed_at timestamptz,
    confirmed_by text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS attestations (
    claim_id        text NOT NULL REFERENCES claims(claim_id),
    verifier_seat   text NOT NULL,
    verifier_family text NOT NULL,
    family_provenance text NOT NULL
                    CHECK (family_provenance IN ('wire','configured')),   -- see §7.3
    restatement     text NOT NULL,          -- the claim in the verifier's own words
    mechanism       text NOT NULL,          -- which lines, which behaviour, why output entails defect
    falsifier       text NOT NULL,          -- what result would have falsified it
    falsifier_kind  text NOT NULL
                    CHECK (falsifier_kind IN ('command','prose')),        -- see §7.2
    -- Harness-produced record of the verifier's re-run (F2). NOT NULL at the COLUMN,
    -- not merely required by the view: a row cannot exist without machinery contact.
    -- F4: the CITED artefact's author is checked by the consumer at write time.
    rerun_artefact_id      text NOT NULL,
    rerun_artefact_version int  NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (claim_id, verifier_seat)
);

CREATE TABLE IF NOT EXISTS seat_posture (
    seat_id            text PRIMARY KEY,
    requires_claim_ref boolean NOT NULL DEFAULT true,   -- default-deny (§3)
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Lane membership is a STORE fact, written by the consumer at arm time (F3, §5.3).
CREATE TABLE IF NOT EXISTS lease_lanes (
    lease_id  text PRIMARY KEY,
    lane      text NOT NULL CHECK (lane IN ('gated','exempt')),
    armed_by  text NOT NULL,                            -- consumer identity that armed it
    armed_at  timestamptz NOT NULL DEFAULT now()
);

-- Admissibility expressed ONCE, here, so the dispatch gate and the close gate
-- cannot drift about what "confirmed" means.
CREATE VIEW claim_admissibility_v AS
SELECT c.claim_id,
       ok.confirmed_now,
       ok.attested,
       ok.decorrelation_provenance,
       (ok.confirmed_now AND ok.attested) AS admissible,
       c.status, c.review_by
FROM claims c
CROSS JOIN LATERAL (
    SELECT
        (c.status = 'confirmed'
         AND (c.review_by IS NULL OR c.review_by > now()))            AS confirmed_now,
        -- Completeness AND decorrelation, in one predicate: an attestation from the
        -- author's own family does not count as an attestation at all (F1).
        EXISTS (SELECT 1 FROM attestations a
                WHERE a.claim_id = c.claim_id
                  AND a.restatement <> '' AND a.mechanism <> ''
                  AND a.falsifier <> ''
                  AND a.verifier_family <> c.author_family)           AS attested,
        -- 'wire' only when BOTH sides are machinery-attested; otherwise the
        -- decorrelation claim is degraded and the sampler weights it up (§7.4).
        -- count(*) = 0 FIRST: bool_and over zero rows is NULL, which would otherwise
        -- fall through to 'degraded' and report a claim with NO attestation as merely
        -- weakly-decorrelated. An aggregate subquery always returns a row, so COALESCE
        -- would never have fired.
        -- The WHERE clause MUST stay identical to `attested`'s: two subqueries measuring
        -- subtly different populations is drift waiting to be resolved in whichever
        -- direction is convenient. Without the completeness predicates an INCOMPLETE
        -- cross-family attestation yields attested=false alongside provenance='wire'.
        (SELECT CASE
                    WHEN count(*) = 0 THEN 'none'
                    WHEN bool_and(a.family_provenance = 'wire')
                         AND c.author_family_provenance = 'wire'
                    THEN 'wire' ELSE 'degraded' END
         FROM attestations a
         WHERE a.claim_id = c.claim_id
           AND a.restatement <> '' AND a.mechanism <> ''
           AND a.falsifier <> ''
           AND a.verifier_family <> c.author_family)                  AS decorrelation_provenance
) AS ok;

-- Seat posture lives behind the DSN, NOT in the seat's env file (§9.3).
CREATE VIEW seat_posture_v AS
SELECT seat_id, requires_claim_ref FROM seat_posture;

CREATE VIEW lease_lane_v AS
SELECT lease_id, lane FROM lease_lanes;

CREATE ROLE arb_gate_reader LOGIN;
GRANT SELECT ON claim_admissibility_v, seat_posture_v, lease_lane_v
    TO arb_gate_reader;                                 -- and nothing else
```

`confirmed_now` and `attested` are exposed separately, not just their conjunction: the gate must
emit `unconfirmed_claim` and `unattested_claim` as **distinct** refusals (§5.1), because the
dispatcher's next action differs — build a probe versus route to verification. A view returning
only `admissible` collapses two different instructions into one unhelpful bounce.

**Decorrelation is folded into `attested`, not checked alongside it (F1).** A same-family
attestation is not a weak attestation — it is *not an attestation*, because the property it exists
to supply is absent. Putting the check anywhere else leaves a state where the view says attested and
the doctrine says void, which is the wedge every other MUST here is written to avoid. Note what this
closes concretely: a `devin_acp.py:144`-style silent model fallback could land a same-family
attestation that satisfied the old view while voiding decorrelation — the failure §7.3's
named-refusal item prevents one layer down, now also caught at the layer that grants admission.

**`decorrelation_provenance` records degradation rather than hiding it.** It reads `wire` only when
the author's family *and* every counting attestation's family are machinery-attested; otherwise
`degraded`. It never blocks — `configured` is the normal case today (§7.3) — but it is a
preferential-sampling input exactly like `falsifier_kind = 'prose'`: where the decorrelation
guarantee rests on self-report, the die lands more often.

**The re-run reference is `NOT NULL` at the column (F2).** Requiring it in the view would leave the
requirement one view-rewrite from disappearing; at the column, an attestation row cannot be written
at all without a harness-produced re-run artefact to point at.

**The GRANT is the trust story in one statement.** The bridge holds `arb_gate_reader`: three views,
SELECT only — no table access, and not the consumer's credential. A leaked or misused bridge
credential can read confirmation state, posture and lane, and structurally cannot mint any of them.
Only the consumer writes `claims`, `attestations`, `seat_posture` and `lease_lanes`. The asymmetry —
bridge reads, only the consumer writes — is the whole trust story of this design in one GRANT
statement.

**Expiry folds in at resolution (MUST).** Admissibility is "confirmed *as of now*". Without this,
an expired accepted-risk is dispatchable-but-uncloseable — a wedge state discovered at the worst
possible time.

## 5. The gate at admission

New module `src/agent_redis_bridge/claim_gate.py`. One insertion in `handle_raw`, placed **after**
the `sender-rejected` check (`bridge.py:1193`, so authenticated sender identity is settled) and
**before** duplicate/budget:

```python
# Posture is READ FROM THE STORE (seat_posture_v), never from a local flag or the
# env file — see §9.3. The resolver answers both questions over one credential:
# "does this seat require a ref" and "is this ref admissible now".
outcome = claim_gate.check(envelope, seat_id=self.agent_id, resolver=self.claim_resolver)
if outcome is not None:
    logger.error(f"[bridge-error] {outcome.code} {outcome.gaps}")
    self.send_reply(envelope, TurnResult(ok=False, result="", error=outcome.as_error()))
    return False
```

There is deliberately **no local posture short-circuit**. A seat that could answer "I am not
posture-bearing" from its own config would be able to disable the gate by editing a file it owns —
the precise failure §9.3 documents in `readonly_gate`. Posture-not-required is a *fact the store
returns*, which also means a store outage refuses rather than silently ungates every seat.

This is structurally identical to the existing `check_usage_budget()` call at `bridge.py:1210` —
compute an error, reply `ok=False`, return `False`. No new plumbing in the request path.

`envelope.py` gains a **type check only**: if `claim_ref` is present it must be a non-empty
string. The envelope layer never decides admissibility.

### 5.1 Refusal codes

House style follows `src/arb_memory/close.py:139,171` — `{"outcome", "exit_code", "gaps"}`.
Distinct codes are mandatory: a store hiccup that reads as a fraud signal trains everyone to
distrust the gate's refusals, which is how gates get worked around legitimately.

| code | meaning | audience / next action |
|---|---|---|
| `missing_claim_ref` | posture requires a ref, none present | dispatcher → exempt lane, build the probe |
| `unconfirmed_claim` | ref resolves, not admissible now | dispatcher → exempt lane, build the probe |
| `unattested_claim` | confirmed, no complete attestation | dispatcher → verification, **not** the probe lane |
| `unknown_claim_ref` | ref does not resolve | dispatcher — typo or stale reference |
| `lane_not_armed_exempt` | envelope presents as exempt traffic; the store records no exempt lease | dispatcher — lanes are armed by the consumer, not asserted (§5.3) |
| `store_unreachable` | authority unavailable | operator — pages; **never** an accusation |

`gaps` names the missing ref **and** the lanes the payload could legitimately take, so a
confused-but-honest dispatcher is routed rather than bounced. The gaps-naming pattern is half of
why `refused_reconcile` worked as a template.

**Fail-closed (MUST):** store unreachable ⇒ refuse. **No caching**, or caching measured in
seconds — at this dispatch volume there is no latency case, and a cached confirmation is a small
replica of the token-staleness problem §9.1 declines to buy.

### 5.2 The dispatch is a stored artefact

**The bus carries a brief artefact ID, not loose text (MUST).** Before send, the brief is written to
the store — harness-hashed and versioned — and the envelope references it. The worker hydrates the
brief itself; the bus never carries the body.

**This completes an asymmetry already in doctrine rather than adding a rule.** CLAUDE.md's
return-channel rule already requires a round's reply to carry `{artefact_id, version, change
summary}` and never the body. Dispatch, however, is still freeform prose: `payload.task` is
validated only as a non-empty string (`envelope.py:61-66`), with `worktree_arm`/`worktree_release`
the sole structured operations. Replies are ID-shaped; dispatches are body-shaped. This closes the
half that was never built.

**It costs no credential widening.** The *worker* hydrates the brief using the seat's own read
credential — `local_read_policy.py:9` resolves `ARB_MEMORY_LOCAL_DSN`, distinct from the writer DSN
and fingerprint-guarded against cross-store reads. The bridge never needs `artefacts` table access,
so `arb_gate_reader` stays SELECT-only on its three views (§4). A design in which the bridge hydrated briefs
would have dissolved that GRANT, which is the trust story.

What this buys, and it is the only version where hashing does real work:

- **The hash binds what was dispatched to what was gated**, immutably. No post-gate edit can slip a
  claim into a brief that already passed admission.
- **The audit trail is exact**: which brief, at which bytes, passed which confirmations.
- **The close reconcile can check that the remediation answers the brief that actually went out** —
  the same approve-by-ID pattern used for spec review.

#### The resolution record is an audit record, never a credential (MUST)

The brief artefact records which claim refs were resolved at dispatch and what they resolved to.
**That record is evidence of what the gate saw. It is not authority, and the close MUST re-resolve
every ref fresh against the view.**

This typing is load-bearing. A stored, immutable snapshot of "these refs were confirmed at dispatch
time" is structurally a **signed claim token wearing a different hat** — confirmation captured at
issuance and consulted later, immune to the retraction and accepted-risk expiry that make
confirmation time-varying here. §9.1 declined to buy that, and it would otherwise arrive free with
the artefact. Keep the distinction sharp and the record is pure gain; blur it and the close begins
deferring to the pinned snapshot, which makes dispatch-time verification authoritative — the one
property this design wants nowhere except the close.

#### The brief carries an assumptions section (honest-limits, not a gate)

A third claim class sits alongside the two this spec already covers. Findings need red probes;
remediations need green artefacts; **readiness declarations need demonstrated preconditions** — and
today they have exactly the structure unverified findings had: a confident statement whose
load-bearing assumptions are nowhere in the artefact.

The brief schema therefore carries an `assumptions` section. Every precondition the brief depends on
is either:

- **demonstrated** — carrying a reference to an artefact showing the precondition holds, or
- **named as assumed** — explicitly listed, undemonstrated, in the bytes.

An empty section means *"this brief has no preconditions"* as an explicit claim, not as a silence.
That distinction is the entire mechanism: an assumption nobody wrote down becomes a schema
violation instead of a surprise.

**Demonstration is vantage-relative (MUST).** A precondition demonstrated from the author's host is
not demonstrated for the worker's. Reachability, credentials, mounted paths, DNS and clock are all
properties *of a vantage point*, and the vantage that matters is the one the dispatched work runs
from. A demonstration artefact must record where it was produced, and a demonstration from the wrong
vantage counts as `named as assumed`, not as demonstrated — the same author-cannot-certify-another-
slice's-behaviour rule this repo already applies to cross-slice claims.

**This is deliberately not a gate**, and §9 is where it belongs in spirit: preconditions cannot be
exhaustively enumerated by machinery, and a gate pretending otherwise would relocate the vacuity
rather than remove it — a brief could satisfy a "has assumptions section" check while omitting the
assumption that actually sinks the round. What the schema buys is narrower and real: the enumeration
is forced to exist in the bytes at send time, so the close reconcile and the spot-check have
something to resolve against, and the failure mode degrades from *invisible* to *reviewable*.

#### Rejected: the orchestrator hashes and re-reads its own dispatch before send

Considered and rejected, recorded because the reasoning generalises.

- **Hashing the brief binds bytes to bytes; it cannot bind bytes to acts.** The failure mode is
  never payload corruption — it is that a brief's *claims* reference verifications that never
  happened, and a hash over the text containing them comes out clean regardless. Worse, it would
  emit a dispatch bearing a verification stamp: a faithfully-executed, script-shaped check attached
  to the property that does not matter. That is
  `docs/defect-classes/verification-is-context-triggered-not-risk-triggered.md` promoted to policy.
- **Re-reading surfaces discrepancies, not absences.** Re-contact catches drift that is *present in
  the bytes* (context says one thing, file says another). An unverified claim leaves no byte-level
  referent to collide with — the brief reads clean, internally consistent, well-formed, and the
  missing thing is a run that never happened. It is an absence in the world, not a defect in the
  document. Worth its zero cost as a prompt-layer nudge; never worth a place in the definition of
  anything.

The transform that survives: whenever a control arrives as *"ask the orchestrator to X before Y,"*
the version that holds is *"make Y's machinery require X's artefact."* Applies to **acceptance**
decisions; see §9.5 and §12 for where judgment deliberately remains.

#### Operational consequence

A store write before every send means **a store outage stops all dispatch, including the exempt
lane** — wider than §5.1's `store_unreachable`, which only bites gated traffic. This is arguably
inherent (the store is also where probe packages land, so a seat that cannot reach it cannot
deliver), but "the gate refuses gated traffic" and "nothing dispatches at all" are different
operational postures and the second must not be discovered during an incident.

### 5.3 Exempt-lane membership is a store fact, end to end

*(F3. §3 defines lanes by capability and §6 arms them server-side, but nothing said how
`claim_gate.check` knows a given envelope is exempt traffic. Default-deny plus an unspecified
resolution is precisely the condition under which an implementer adds a convenient flag — so it is
specified here rather than left to be filled in wrong.)*

**Exempt status resolves from the consumer-armed lease record in the store, never from the
envelope (MUST).** The envelope references the lease it runs under; the gate asks `lease_lane_v`
whether *the store* records that lease as armed `exempt`, over the same `arb_gate_reader`
credential that answers posture and admissibility.

Resolution order, and the defaults are the point:

1. No lease reference, or a lease the store does not record ⇒ **gated traffic**. Needs a claim ref.
   Silence is never exemption.
2. Lease recorded `gated` ⇒ gated traffic.
3. Lease recorded `exempt` ⇒ exempt traffic; no claim ref required.
4. Envelope presents as exempt but (1) or (2) holds ⇒ refuse `lane_not_armed_exempt`.

An envelope field describing the lane may exist for routing and logging, and it is never consulted
for admission — the self-attestation hole would otherwise re-open at the front door, which is the
one place default-deny cannot save you.

**Implementation note — leases are not in Postgres today.** `WorktreeLeaseStore` is a *filesystem*
store rooted per agent (`bridge.py:1869`, `worktree_lease.py:56-59`); nothing about a lease
currently reaches the DSN. So this is not "read the existing lease record": the consumer **writes a
`lease_lanes` row at arm time**, and the filesystem store keeps doing what it does (worktree
lifecycle, TTL, reclaim, tombstone). Two records, one fact each — lifecycle on disk, lane authority
in the store. The gate must not read the filesystem lease: it is on the seat host, editable by the
seat, and would reproduce §9.3's flaw at a new site.

## 6. Exempt lane mechanics

**Enforcement is credential-shaped, not flag-shaped.** The exempt worktree's checkout has no push
rights to the shared remote (read-only deploy key), with full local execution freedom. "Never
mergeable" is then an *authentication failure* at the same choke point as the merge gate, rather
than a semantic the release path must remember to honour. `worktree_lease` tombstoning reverts to
what it is good at: cleanup.

**Lane parameters are set server-side (MUST).** The consumer arms the worktree; the dispatcher
never passes the flag. `worktree_arm`'s payload schema is already closed —
`set(payload) - {"operation","worktree","lease_ttl","run_id"}` non-empty raises
`worktree-arm-invalid-schema` (`bridge.py:1849-1851`) — so this requires **no** schema widening.
The flag simply never becomes a payload field.

**The probe artefact is a package, not a transcript.** It carries probe source, fixtures, seed
data and harness config, *plus* the run log proving red. The run log proves it went red; the
package makes red reproducible by anyone.

This is load-bearing: the tombstone destroys the worktree, and an oracle that cannot be re-executed
is not an oracle. The repro does double duty — it confirms the finding *and* becomes the acceptance
oracle for the remediation and the target of the close-time spot-check. Store only a transcript and
both of those die with the lease.

**The probe re-lands through the gated lane.** Probe code dies with the exempt worktree and is
rehydrated from the artefact as part of the remediation diff, becoming a permanent regression test.
The exempt lane stays sterile — nothing it produced merges directly, not even the probe — the
remediation carries its own oracle into the suite, and the standing mutation gate (§10, slice 3)
gains a genuinely behaviour-linked test to protect.

## 7. Verification identity

*(Added at review, 2026-07-26. Closes the hole left by §4: `status` had no author — nothing said
what makes the consumer write `confirmed`.)*

### 7.1 The verifier is a second identity, and needs shaping against the same disease

A verifier seat has the same attention architecture as the orchestrator: a model reading a clean
probe attached to a plausible claim — the low-surprise state, relocated. A checkbox attestation
("re-ran, confirms") is a second rubber stamp with a different ID, and the gate cannot tell it from
the real thing.

**The deliverable is typed so it cannot be produced without the cognition (MUST).** The attestation
carries the claim restated in the verifier's own words, the mechanism by which the probe
demonstrates it (which lines, which behaviour, why this output entails that defect), and what
result would have falsified it. None of that survives a skim. Incomplete attestations are
unresolvable at the view — see the `EXISTS` clause in §4.

**The re-run must be machinery-attested, not asserted (MUST, F2).** The prose fields force the
cognition; they do not evidence contact with ground truth. "Re-ran and confirms" written into a text
column is participant-attested — the exact provenance class this design refuses for the *author's*
evidence in §6, and there is no principle under which the verifier gets the weaker standard. The
verifier's re-run therefore routes through the harness like every other execution, and the
attestation carries `rerun_artefact_id` + `rerun_artefact_version` as `NOT NULL` columns. Schema
before this fix had the cognition-forcing half without the ground-truth half.

**The cited re-run must itself be harness-authored (MUST, F4).** `rerun_artefact_id NOT NULL` proves
a *pointer exists*, not that what it points at is machinery-produced. A verifier could store its own
transcript and cite that, reinstating self-report one pointer deeper — the same defect as F2, moved
rather than fixed. The store already records artefact authorship (`schema.sql:11-12`: `source`,
`author`), so the check is cheap: **before INSERTing an attestation row the consumer resolves the
re-run artefact and refuses unless its `author` is on the harness-identity allowlist.** This is
enforced at the consumer's write path rather than as a column constraint, because it is a
cross-artefact property the `attestations` table cannot see; that placement is a genuine weakening
versus F2's column-level `NOT NULL`, and is recorded as such rather than glossed. Same reasoning as
F2 verbatim: there is no principle under which the verifier's evidence gets a weaker provenance
standard than the author's.

Honest limit: a schema enforces **presence**, not non-vacuity. A padded restatement satisfies
`<> ''`. §7.2 is the field that escapes this; §9.4 records what remains.

### 7.2 Executable falsifiers

`falsifier_kind = 'command'` wherever expressible. An executable falsifier gives the spot-check a
mechanical target — run it, confirm it goes the other way — converting the one anti-vacuity field
with real teeth from a reading judgment into a check. Prose is permitted only where the claim
genuinely cannot be expressed as a probe.

**`falsifier_kind = 'prose'` is a preferential-sampling flag, not merely a permitted variant.**
Claims whose falsifiers could not be mechanised are precisely where reading judgment is still
load-bearing, so the die lands there more often.

### 7.3 Lineage, assignment, provenance

**Different model family from the finding's author (MUST).** Shared priors are what pass correlated
errors, and correlated errors are the class surprise-gated attention misses in every seat
simultaneously.

**Assignment is made by the consumer at confirmation time (MUST)**, never by the orchestrator at
dispatch — both so the author cannot predict its verifier, and because verifier selection is itself
an acceptance-adjacent judgment just removed from the warm seat.

**The check is on the outcome, not the assigner's intention (F1).** `claims` records `author_seat`,
`author_family` and `author_family_provenance`, and the decorrelation predicate lives inside
`attested` (§4) — so what is verified is that the attestation which *actually landed* came from a
different family, not that the consumer *meant* to route it that way. Letting assignment stand for
outcome would be the one place this design trusted an intention, and assignment is exactly where a
silent model fallback can make intention and outcome disagree.

**Family provenance.** The doctrine elsewhere prefers machinery-attested facts to
participant-attested ones. That preference is only partly satisfiable today, and the spec says so
rather than implying otherwise:

- Model identity currently flows **outward**. The bridge *sets* the model — `--model`
  (`bridge.py:4096-4150`), `DEFAULT_CODEX_MODEL` (line 116), `session/set_model` pushed to engines
  (`gemini_acp.py:111`, `grok_acp.py:148`, `devin_acp.py:186`). For most engines the configured
  value is the only value, i.e. self-report with extra steps.
- The partial exception is Cursor, whose session returns a `models` list the bridge resolves
  against (`cursor_acp.py:124`).
- Therefore `family_provenance ∈ {wire, configured}`, recorded per attestation. Mismatch refusal is
  armed only where wire identity exists. Where it is `configured`, the decorrelation guarantee is
  weaker and the record shows it.
- A **static per-family table is rejected on precedent**: `bridge.py:1881` documents a pinned
  per-family table wrongly forcing stateless bounces on non-oneshot asdk seats, fixed by
  advertising actual capability as ground truth (`thread_resume`, line 1884).

**Named-refusal candidate (in scope, slice 1):** `devin_acp.py:144` logs *"devin set model failed,
continuing with session default"* and proceeds. The seat then runs on an unknown model while its
config claims otherwise — the exact "config says one thing, connection is another" case. Silent
fallback must become a named refusal, or a cross-family assignment routed to that seat can be
same-family with nothing saying so, voiding the decorrelation the assignment exists to buy.

### 7.4 No signature graduates out of the sampling pool

The consumer's randomised spot-check has attestations in scope: occasionally reproduce a claim
*despite* a clean attestation on it. This is not distrust of verifiers specifically — it is the
uniform rule. The moment one identity's attestation becomes final-by-convention, a trust root has
been re-created with better paperwork.

The spot-check's job is narrow and unchanged in kind: not "was this verified" but **"does this
probe establish this claim"**.

## 8. Economics: why the requirement keys to lane, not severity

**The attestation requirement is keyed to what the remediation can reach, never to severity
(MUST).** Any claim whose remediation runs on the gated lane needs an attestation; exempt-lane work
never does. Severity governs queue order only.

Keying to P0/P1 would put a severity judgment — made by a model seat reading a well-written
finding — in the position of deciding whether the gate applies. That is the same structure as the
orchestrator deciding whether to verify, one field over, and a `P2` label becomes the cheapest way
past the gate. Not maliciously: the honest low-surprise call on a polished finding is "this looks
like a P2."

Moving severity assignment off the finder does not fix it. The consumer-side assigner is still a
model in a low-surprise state whose honest misjudgement opens the cheap path — which would require
typed severity rationales and sampling of severity calls, i.e. rebuilding the attestation apparatus
to protect the parameter that decides whether the attestation apparatus applies. Lane-keying
deletes the recursion: the gate keys to a routing fact, and severity's "costs latency, not truth"
justification survives without an asterisk.

**The actual economics:** lane-keying prices attestation **per remediation, not per finding**. A
large recall surface floods the finding queue, but findings never dispatched never need attestation.
A P2 that sits triaged costs nothing until someone judges it worth an implementer — at which point
the attestation is a small fixed fraction of the round already being paid for. Verification spend
scales with work dispatched, which is the proportionality wanted.

**If volume bites, the knob is attestation *depth* — a lighter schema for low-blast-radius claims —
never exemption.** Adjust cost inside the requirement. A bypass, once it exists, is where honest
misclassifications pool.

## 9. Stated explicitly, not solved

### 9.1 TOCTOU layering is deliberate

Confirmed at dispatch, expired by close: fine, and intended.

- The **dispatch gate is the economy gate** — it stops an implementer being consumed on an
  unconfirmed finding.
- The **close reconcile is the authority gate** — it re-resolves everything at merge time.

Staleness between them costs at most one wasted round, never a wrong merge. Two consequences, both
binding: nobody needs to build dispatch-time revocation (the close covers it), and nobody may
weaken the close because "dispatch already checked" (dispatch was never final).

This is also why **signed claim tokens were rejected**. A token is a snapshot of "confirmed" at
issuance, verified offline later — but confirmation here is *time-varying*, since accepted-risk
expiry flips a finding back to unconfirmed. Tokens would need revocation or short TTLs to be
correct, and revocation checking is a store lookup: the chosen design plus key-distribution
machinery. Bearer tokens also replay — nothing structural stops a token minted for one dispatch
validating another. Tokens try to make dispatch-time verification authoritative, which is a
property this design does not want anywhere except the close.

### 9.2 Exempt-lane output does reach the merge path — via review

"Exempt-lane output never reaches the merge path" is true of the **lane** and deliberately false of
the **artefact contents**. The probe package is input to a later gated task, so the remediation
implementer executes code that originated in an exempt worktree. That is fine: it arrives via the
harness store with provenance and is reviewed as part of the remediation diff under
author-non-quorum like any other change.

Stated explicitly because leaving it implicit is the kind of ambiguity a future session resolves in
whichever direction is convenient.

### 9.3 The credential residual

`arb_gate_reader`'s connection string lives on the seat host. The property achieved is therefore
**posture is centrally administered and drift is visible** — not *unbypassable*. The threat model
is honest drift, not sabotage, which makes this the right trade; but the spec says it rather than
implying the gate is airtight.

Related: the gate check and posture config belong to the **consumer's deployment**, resolved from
the store — not to code the orchestrator's session can edit and restart. Note the existing
precedent has exactly the flaw being avoided: `readonly_gate`'s posture flag is read at
`bridge.py:447-448` from `env.get("ARB_REQUIRE_READONLY_TOOLS") or os.environ.get(...)`, and per
`bridge.py:101-114` the env file resolves to `.env` in the app repo worktree — a file the seat can
edit. A control the session can reach is a convention with better formatting.

### 9.3a The mid-turn control residual (`steer` / `cancel`)

**Named residual (Slice 1c impl r1, P1-4 decision (b)).** `handle_raw` routes `steer` and
`cancel` to `handle_control` **before** the claim-gate block. `handle_control` checks only that
the sender policy is not `reject` (plus panel-input lock and an active task target). It does
**not** re-check the admitted parent task's claim, and it does **not** require the steerer to be
the original admitted sender.

**Consequence, stated plainly:** once a turn has been admitted, **any non-rejected sender can
steer that running turn** (inject further instructions into the engine). The control path cannot
start work from cold — there is no active task without a prior admitted `request` — which is why
this is residual rather than a cold-start bypass. `cancel` remains on the same path so operators
who did not own the original dispatch can still abort.

**Why not bind steer to the parent claim in Slice 1c:** the gate is the *economy* gate for
whether work *starts* (§9.1). Binding mid-turn control would need new envelope/claim semantics
(steerer presents `claim_ref`, or steerer ≡ original sender) that no plan or §5 refusal code
names, and would risk locking out legitimate multi-orchestrator steer and operator abort. Those
bindings remain open design, not silent default.

### 9.4 A falsifier can itself be vacuous

A command that fails for reasons unrelated to the claim satisfies "runs and goes the other way."
The spot-check running it catches the broad class mechanically, but the falsifier is the verifier's
work product, and the same rule applies to it as to everything else: no artefact type graduates out
of the sampling pool (§7.4).

### 9.5 Green is necessary, never sufficient

Dispatch-time gates govern whether work *starts*; they do not touch real-finding/wrong-fix, where
the defect is in what comes back. That is caught at the other end: probe green **and** full suite
green **and** mutation gate over changed tests **and** the remediation diff through panel review
under author-non-quorum. Diff review is the one place reading judgment legitimately remains
load-bearing, and it is safe there because it is decorrelated reading rather than self-assessment.

## 10. Slicing

**The gate cannot ship before the exempt lane exists.** Default-deny with no lane is a total
dispatch lockout — and the first thing anyone would need to dispatch is the probe that makes the
gate satisfiable. The lane is therefore inside slice 1, not after it.

| Slice | Contents |
|---|---|
| **1 (this scope)** | `claims` (incl. author identity) + `attestations` (incl. re-run refs) + `seat_posture` + `lease_lanes` + the three views + `arb_gate_reader` GRANT; `claim_gate.py`; `handle_raw` insertion + 6 refusal codes; `claim_ref` type check in `envelope.py`; exempt-lane arm path with push-less credential **and the consumer-written `lease_lanes` row (§5.3)**; probe-package artefact schema; consumer-side verifier assignment + family-advertisement plumbing; harness re-run path for verifiers **and the consumer-side harness-identity check on the cited re-run artefact (§7.1, F4)**; `devin_acp.py:144` named refusal; **brief-artefact dispatch (§5.2)** — store-before-send, envelope carries the brief ref instead of the body, worker-side hydration via the seat read DSN |
| **2** | Close-reconcile re-resolution (expired accepted-risk → blocking gap, requiring fresh demonstration or fresh human override); randomised spot-check sampler with `falsifier_kind='prose'` weighting |
| **3** | Probe re-landing convention; standing mutation gate over changed test files in CI |

Slice 3's mutation gate is independent of everything above and justified on its own evidence: four
test suites in one phase stayed green with the behaviour under test deleted, and *every report
about them was accurate*. No amount of orchestrator verification reaches that class.

## 11. Testing

- **View semantics** (`tests/arb_memory/` style): admissibility truth table — unconfirmed,
  confirmed, retracted, `review_by` boundary (`> now()` exactly at expiry), attestation present but
  incomplete, attestation absent, **attestation complete but same-family as the author** (must read
  not-attested, not merely flagged), **attestation complete but re-run artefact missing** (must be
  unwritable at the column, so the assertion is that the INSERT fails — not that the view hides it),
  and `decorrelation_provenance` reading `degraded` when either side is `configured`.
- **Enum drift**: each CHECK-constrained column rejects an out-of-domain value. The views trust
  these strings; an unconstrained text column would be a silent drift channel into `attested`.
- **Re-run provenance** (F4): an attestation citing a re-run artefact authored by the *verifier*
  (not a harness identity) is refused by the consumer at write time. Assert the refusal names the
  offending author, and assert the positive case admits.
- **Population alignment**: an *incomplete* cross-family attestation must yield `attested = false`
  **and** `decorrelation_provenance <> 'wire'`. This is the regression test for the two subqueries
  drifting apart again.
- **Refusal codes** (`handle_raw` tests): one per code in §5.1, asserting both the code *and* that
  `gaps` names the legitimate alternative lanes. Explicitly assert `store_unreachable` is never
  emitted as a confirmation failure.
- **Fail-closed**: resolver raising ⇒ refuse, never admit.
- **Lane deny-proof**, mirroring `tools/seat_deny_proof`: attempt a push from an exempt worktree
  against the shared remote and assert it fails at the remote, not at a local check.
- **Probe rehydration**: a package stored by an exempt lane, after lease tombstone, re-executes and
  reproduces red — and the verifier's `rerun_artefact` reproduces the author package's result,
  pinning F2's requirement to an observable rather than a column being populated.
- **Lane resolution** (§5.3): an envelope presenting as exempt against a lease the consumer never
  armed exempt is refused `lane_not_armed_exempt`; an envelope with no lease reference is treated as
  gated and refused for a missing claim ref; and a `lease_lanes` row written directly on the seat
  host cannot influence the decision (the gate reads the store, never the filesystem lease).
- **Family mismatch**: where `family_provenance = 'wire'`, a configured/wire disagreement produces a
  named refusal rather than a silent preference for either source.
- **Brief-artefact dispatch** (§5.2): an envelope carrying a body instead of a brief ref is refused;
  the brief the worker hydrates hashes to the brief that passed admission; and — the one that
  matters — **the close re-resolves claim refs and blocks on an expired one whose stored dispatch
  record says `confirmed`**, proving the resolution record is being read as audit and not as
  authority.

## 12. What remains judgment

Triage and prioritisation — which findings get probe dispatch first, and how work is sequenced.
Safely so: getting it wrong costs latency, not truth. That is not a demotion of the context-rich
seat; it is pricing it correctly.
