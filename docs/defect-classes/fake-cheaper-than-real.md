# Fake cheaper than real (on the load-bearing dimension)

**The class.** A test double that is cheaper or simpler than the real component **along the dimension that
actually matters** certifies a behaviour the real component does not have. The bug lives precisely in the gap
between the fake and the real thing — exactly where the test is blind.

## Detection move

For any pooled / adapter / wrapper / protocol-implementer / framework-fronted layer, **name the costly
dimension the fake is cheaper on, then add at least one test that preserves it** — don't just exercise the
interface:

- **Interface completeness** — drive the real downstream consumer (e.g. the SDK's own resume path) against
  the real impl, not a mock that happens to implement the method the real impl forgot.
- **Latency** — use a SLOW-`start()`/slow-op fake (or the real component) so timing-dependent behaviour
  (admission-thread blocking, lock contention) surfaces.
- **State / answer-reachability** — exercise the real reachable state (real filesystem, real config dir, real
  serialization), not a fake that doesn't share it.
- **Connection / transaction semantics** — run at least one test against a connection configured *like
  production* (non-autocommit, real lifecycle) — see the fixture face below.

If a "real-X" test passes by substituting a renamed/cheap fake, it defeats the purpose — verify it touches
the real type AND preserves the dimension.

## Why it escapes

Fakes are written to satisfy the code-under-test on the happy path, so they are implicitly cheaper/simpler
than reality wherever the author didn't think the difference mattered — which is exactly where the escaped
defect hides. Execution-primary validation (run the real thing) is the backstop a cheap-fake test
structurally cannot be.

## Two faces (different mechanisms, different hunts)

- **Fixture face** — the fixture *supplies* the property the code fails to:
  [`fixture-supplies-what-code-lacks.md`](fixture-supplies-what-code-lacks.md).
- **Framework face** — the framework rejects the violation before the test reaches the code:
  [`test-behind-framework-drive-directly.md`](test-behind-framework-drive-directly.md).

## Instances (the corpus)

- **ED-001** — `FileSessionStore` missing `list_subkeys`/`list_sessions`; `ScrubbedSessionStore` falsely
  advertised them; 365 green tests over a broken resume path, caught only by the LIVE run. (interface)
- **EnginePool admission-thread flaw** — the fake engine's instant `start()` hid that the real agent-sdk
  `start()` (~30–90s) blocks the admission thread under the pool lock. (latency)
- **DC-004** — the held-out oracle was filesystem-reachable by the process under test. (state)
- **ARB Memory Phase 3 autocommit** — a test fixture's autocommit hid that production connections never
  committed: a store that didn't store, 90+ green tests, on the public auth boundary. (transaction semantics —
  the fixture face)
