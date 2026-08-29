# The primary path was the unreviewed path

A **process / coverage** defect class (distinct from the test-hollowness classes): the slices test the *easy*
door thoroughly and defer the *hard* one — and the hard one is usually the **primary, high-frequency path**.
"We reviewed it rigorously" then means "we reviewed the door that was easy to review," while the path that
actually carries the load was never executed end-to-end.

## The mechanism

Integration tests for the hard path get deferred *precisely because they're hard* (they need real distinct
processes, real env, real cross-component wiring), so the build accretes thorough coverage of the components
and the *convenient* integration, and the inconvenient-but-primary integration is the one nobody ran. The gap
hides because every individual piece is green and the *other* door demonstrably works.

## Canonical instance (ARB Memory)

Phases 0–3 built the store, bus transport, audit, and the **external** MCP-OAuth door — and that external door
was panel-reviewed to exhaustion and works. But the **internal** path — *seats over the bus*, which the
architecture (§5) names the **high-frequency primary** clients — was **never executable**: there was no
seat-facing handle, no consumer running on the bus, and `run.py` imported `psycopg`+`mcp` at top level so
`python -m arb_memory write` would crash at import in a lean seat env. The whole memory layer was "built and
reviewed" for the door that was easy to test; the door that matters most for *using* it had never run cross-
process. It surfaced only when someone asked the plain question — "have we actually run ingest+recall across
seats?" — which no green suite had answered.

## Detection move

- At the close of a build, **name the primary, highest-frequency path and ask: "has it run end-to-end, as
  the real actors, in the real environment?"** — not "do its components pass," and not "does an *adjacent*
  path work." If the answer is "we tested the other door thoroughly," that's the smell.
- **Make the hard integration test a first-class deliverable of the slice that builds the path, not a
  deferred follow-on** — deferral is how the primary path becomes the unreviewed one.
- When a path can't even be *exercised* (the package won't import in the target env, nothing is running to
  serve it), that is itself the finding — surface it loud; don't let "the components pass" stand in for "the
  path works."

Related: the e2e-hollowness classes ([`fake-cheaper-than-real`](fake-cheaper-than-real.md),
[`fixture-supplies-what-code-lacks`](fixture-supplies-what-code-lacks.md)) describe *why a green integration
test can prove nothing*; this class describes *why the integration test that mattered was never written*.
