---
# SEED — copy this file to `docs/posture-oracle.md` in your repo at adoption, then own it.
# Fill in `owner` and keep `last-reviewed` current; this seed's date is its authoring date, not yours.
owner: "<set at adoption — the role/person accountable for keeping this list current>"
last-reviewed: "2026-06-16"   # update on every review/cadence pass
review-cadence: "quarterly, and on every posture incident (grow-when-bitten)"
---

# Posture oracle — spec-INdependent posture checklist

This is the **spec-independent** check (#3) of the autonomous-mode Stage-5 gate and Stage-6 morning
pass (see `skills/autonomous-mode/SKILL.md § Drift-against-spec + posture-conformance`). It runs
against the integrated diff **regardless of whether the spec or the Stage-0 gate named any of these
classes.** Its whole job is to catch the *recurring* posture classes the spec/gate omitted
(known-unknowns); novel domain-specific dimensions are the Stage-0 gate's job, not this list's.

## Mechanism — two tiers (this is what makes it an *oracle*, not just another model pass)

Each class is tagged **[M]** or **[J]**:

- **[M] Mechanical tier — deterministic, model-INdependent.** Decidable by regex / config / AST.
  Run as a scan with a documented false-positive profile; independent of every reviewer model by
  construction. **Spec-acknowledged-exception path:** if a flagged deviation was *explicitly
  enumerated and accepted* in the Stage-0 posture-baseline gate, downgrade it from REQUEST CHANGES
  to a **logged note** that cites the accepting Stage-0 line — no bare overrides. (A scanner can't
  read intent; without this, the mechanical tier blocks human-approved, spec-stated posture.)
- **[J] Judgment tier — model-based, must be DECORRELATED.** Needs semantics, can't be regex'd.
  Run it on a model **outside the Stage-5 voting quorum** — **not codex, agy, or cold-Opus** (all
  three are voting seats; routing the oracle to one re-creates the correlation the oracle exists to
  break). **A valid decorrelated adjunct is a real non-quorum bridge seat** (pi-rpc / gemini per
  `using-agent-bridge`) — **not bare-API, not a voting-seat family, not a same-family substitute
  reached for under park-pressure.** **If no such seat is available, this tier does NOT run
  correlated — it PARKS** (`parked-unverified-posture-judgment`; the feature stages whole), per the
  SKILL's third open limitation (a *delivery* limit, not a faked check). **At the Stage-6 morning
  pass the human runs this tier themselves** — they are the decorrelated check, so park does not
  apply there.

**How to use:** for each class, inspect the diff — "does this change touch it, and is the posture
correct?" Any unsatisfied line → REQUEST CHANGES (remediate or park), except an [M] flag covered by
a Stage-0-acknowledged exception → logged note.

**Maintenance contract:** this list must stay coextensive with `§ Posture detection`'s taxonomy, or
carry an explicit "handled by X" justification for any category it delegates. **Every posture issue
that slips past this list becomes a new line here** — an oracle that only grows when bitten stays
current; one written once and trusted forever silently stops covering the real threat.

## Checklist (seed — extend per repo)

### How the system defends itself
- [ ] **[M] Transport / TLS** — no new plaintext transport; TLS/bus-auth on every new connection (DB, cache, queue, external API).
- [ ] **[M] CORS policy** — no new CORS-open / wildcard-origin surface; origins explicit and minimal.
- [ ] **[M] Auth on every exposed endpoint** — every new route / handler / surface requires authentication; no accidentally-public endpoint. *(Route-table-vs-middleware scan is mechanical; "is this auth correct for the intent" is [J].)*
- [ ] **[J] Input-trust** — no unsafe deserialization/parsing reachable from external input; no command/template/SQL execution from externally- or sender-controlled fields; sender-policy / `AGENT_TRUSTED_SENDERS` not weakened.
- [ ] **[M] Secrets / tokens** — no secrets or bearer tokens written to logs, error messages, fixtures, or committed config (regex/grep). *(Secret storage/rotation design changes are [J].)*

### Who can do what
- [ ] **[J] Authorization / permission scoping** — new surfaces enforce the intended permissions/roles/scopes; no privilege escalation, broadened ACL, or tenancy-isolation bypass.

### What data leaves or is destroyed
- [ ] **[M] Egress targets** — no new external egress destination (host scan) without authorisation.
- [ ] **[J] PII / sensitive-field handling** — no new logging/persistence of PII or sensitive fields; retention/deletion policy not silently changed. *(What counts as sensitive is contextual → judgment.)*

## Delegated categories (NOT checked here — handled by named mechanisms)
- **How the pipeline governs itself** (gate/quorum/taxonomy/reviewer config) → caught by the
  **on-sight guardrail diff-scan** (`§ Posture detection`: "treat any edit to
  gate/quorum/taxonomy/reviewer config as posture-class on sight"). Any such edit is posture-class by
  the commission predicate; it never reaches this oracle as "ordinary."
- **Blast-radius widening** (migrations, schema DDL, DROP/DELETE/TRUNCATE, deploys, external
  publishes) → caught by the **irreversible-signature scan** at Stage 5. Egress widening specifically
  is also covered by the egress line above.
