# Refusal is ambient — assert the code, never the bare refusal

**The class that makes defence-in-depth individually untestable.** In a default-deny
architecture, refusal is the *ambient* outcome. Remove any single mechanism and control usually
falls through to another mechanism, which refuses for its own reason — so **"the gate said no"
carries almost no information about whether the mechanism under test still exists.** The layers
mask each other's absence. This is endemic to the architecture, not bad luck: the more thorough
the defence-in-depth, the more reliably each layer's deletion is concealed by its neighbours.

**Red is not the assertion. Red-for-the-stated-reason is.**

This is the specific, demonstrated form of the general residual recorded at
`docs/superpowers/specs/2026-07-26-bus-side-gate-design.md` §9.4 — *"a falsifier can itself be
vacuous: a command that fails for reasons unrelated to the claim satisfies 'runs and goes the
other way'."* See "Proving instance" below.

## The detection move

- **On any deny-proof or gate test:** *if I delete the mechanism this test names, does the test
  go red — or does a different layer refuse and keep it green?* Assert the **code**, not
  `is not None` / `ok=False` / "an exception was raised".
- **On any layered system before writing its tests:** *how many distinct ways can this input be
  rejected?* If more than one, a bare-refusal assertion is untestable by construction.

## Canonical instance (2026-07-26, the bus-side gate's own deny-proof)

The first deny-proof for `claim_gate.check` asserted only `outcome is not None` across the
non-admissible matrix. Inject-revert:

| Injection | Result | Why |
|---|---|---|
| `if not found.attested:` → `if False:` | **red** — "was admitted" | last layer; nothing below it to refuse |
| `if declared_lane == "exempt":` → `if False:` | **green — MISSED** | fell through to claim resolution, refused `missing_claim_ref` |

Real refusal, wrong mechanism, green suite. The lane check could have been deleted entirely and
the deny-proof would have certified the gate.

Fixed by asserting the code each mechanism produces; all three injections then went red with the
mechanism named. Note the irony worth keeping: this was the deny-proof *for the gate designed to
stop unverified claims*, and it was itself vacuous.

## The enabling condition — and why it is not free

This guard is only writable because spec §5.1 gives each mechanism a **distinct refusal code**.
Those codes were justified on dispatcher-routing grounds (the dispatcher's next action differs
for `unconfirmed_claim` vs `unattested_claim`). They pay for themselves a second time here:
**without per-mechanism codes, deny-proofs on a layered gate are structurally unwritable.**

Design consequence, worth applying to the next gate before it is built: *give every refusal path
its own name at design time, or accept that its tests cannot distinguish presence from absence.*

## Enforced by machinery

`tests/defect_hunts/test_gate_assertions.py` — AST-walks the listed gate test modules and fails
any `test_*` function that asserts a bare refusal with no accompanying code assertion. It carries
its own inline inject-revert (`test_the_guard_itself_is_not_vacuous`), because a guard against
vacuous tests that is itself vacuous is the same defect one level up.

Add new gate test modules to its `GATE_TEST_FILES` tuple. The guard is opt-in by design:
`is not None` is only suspicious on a gate path.

## Proving instance for §9.4

The spec predicted this residual and specced the machinery to catch it systematically — the
randomised spot-check sampler (§7.4, slice 2, not yet built). It fired in the wild on day one and
was caught instead by *running the thing and reading the disagreement*. Both halves matter: the
residual is real and not hypothetical, and the machinery that will catch it routinely is already
designed. File alongside
[`deny-proofs-need-adversarial-verification`](deny-proofs-need-adversarial-verification.md) — same
family, one level deeper: that entry says prove your proof goes red; this one says prove it goes
red *for the right reason*.
