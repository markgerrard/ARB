# arb-close-discipline — staging plan, provenance, serving path

**Not served.** `arb-close-discipline.md` in this directory is the SERVED copy — every byte
of it is injected into a warm seat's system prompt. This file holds the parts that must NOT
be injected: the bullets that are not yet live, and the reasoning behind them.

Reference copies in ARB Memory: **`art-cb57c7a0faa7e32a` v4** (the prompt block, live +
staged) and **`art-fe4f19c5ab1e0d87` v2** (the deployment note). Always fetch v2 or later —
both v1s name the repo "AgentBridgeRedis"/"agentbridgeredis", which does not exist, so v1's
symlink command produces a dangling link and thus the exact quiet failure the note warns
about. v1 also documented a `claudeya` body without the readability guard and asserted that
both serving routes are byte-identical, which is not yet true.

## Why the served file is a SUBSET of the artefact — staging, not drift

The artefact carries bullets under two headings — "Live bullets (ship now)" and "Staged
bullets". Only the live bullets are in the served file (count-free phrasing on purpose: a
promotion moves a bullet between headings without this sentence rotting). This is the
sequencing rule, and it is deliberate:

> Never ship a bullet asserting machinery that is not yet live — a bluffable claim teaches
> the seat the prompt lies, which is worse than silence.

`claudeya` on an ungated pipeline is the maximally bluffable configuration: dangerous perms
plus a prompt asserting gates that don't exist. Nothing stops a merge, the seat observes
that the promised reconcile never bounces anything, and it learns to discount the block.

**So the drift boundary reads:** served ⊆ artefact is correct while gates are pending.
served ≠ artefact on a LIVE bullet is drift and must be fixed in the same motion.

## Staged bullets — each promoted only when its trigger fires

Promote by moving the bullet into `arb-close-discipline.md`, in the same commit that lands
the gate it describes. That is what makes the sequencing rule reviewable.

| Trigger | Bullet |
|---|---|
| gate (1)+(2) live | Every evidence-bearing claim forwarded must be typed [E] and cite a resolvable artefact ID written by the execution harness (uploaded_by allowlist). A claim without an artefact reference does not advance — restate as [U] or return to the seat. Remediated and accepted-risk dispositions require the same artefact reference. |
| gate (3) live | Expect one randomised [E] claim per close to be reproduced by the consumer. A failed repro writes a defect-corpus entry against the originating seat. |
| second occurrence of host-scope error | Evidence is scoped to the environment that produced it. Suite counts, timings, and totals are host-specific facts unless the artefact records the environment and the claim explicitly asserts portability. |

The host-scope bullet is one occurrence away: the `933 passed` portability error (asserted as
a portable expectation in a round-4 review brief when it was host-specific) is logged to the
defect corpus as the FIRST occurrence. A second earns the bullet.

## Provenance of the live bullets

- **Proposal-not-close** and **no-polish-exemption**: Opus 5 warm-seat verification-skip
  episode, Jul 2026 — surprise-gated checking, 16 ungated merges, 4 suites green over
  deleted behaviour, xhigh reasoning made no difference, and two memory corrections failed
  to bind.
- **Pinning test / green-suite**: earned by the SECOND recurrence of
  green-suites-as-false-evidence — round-4 grok "K2", where the untested `conn.autocommit`
  line on production's IDLE connection left 64 tests green while the lane reverted to the
  outage shape. Fixed by a factory using a server-side `search_path` so the connection
  arrives IDLE exactly as production's does.
- **Unread oracle**: earned by the SECOND logged occurrence of
  `verification-is-context-triggered-not-risk-triggered` (ARB-B19(a)) — 2026-07-26 seat
  transfer hash-check red against a wrong preimage, oracle unexamined; 2026-07-29 the
  false ARB-B15, filed step-for-step the same way without reading the 20-line
  `src/arb_memory/hash.py`. Promotion co-signed 2026-08-01 (owner decision sweep);
  wording owner-approved verbatim. Asserts no machinery, so the sequencing rule is
  satisfied trivially. No dedicated detector anchor: the served sha256 pin (A7) already
  pins the bullet byte-exact; the three runtime anchors detect block PRESENCE, which
  this bullet does not change.
- **Tree provenance**: earned by the SECOND logged occurrence of
  `workdir-mutated-while-run-in-flight` (ARB-B1) — 2026-07-24 benchmark contamination
  (mid-flight workdir edits produced a mixed result set that read as seat flakiness);
  2026-07-29 the voided shr-s2 suite run (checkout during an in-flight full-suite run,
  result discarded). Owner co-signed the wording 2026-08-01, and RE-co-signed the
  endpoint-honest rewording the same day after the four-seat panel found the original
  clause ("can establish that it didn't") claimed more than an endpoint check delivers —
  the served line now says the stamp is evidence the ENDPOINTS matched, matching the
  mechanism's documented limit exactly. This bullet ASSERTS MACHINERY — the runner and
  its stamp — so per the sequencing rule it ships in the same commit as
  `scripts/tree-provenance-run`. Policy half of the same item: a
  background run gets its own disposable worktree pinned to the commit under test —
  one worktree, one writer. Full class + detection move:
  `docs/defect-classes/workdir-mutated-while-run-in-flight.md`. No dedicated detector
  anchor, same rationale as the unread-oracle bullet.

Accretion discipline (Cherny ablation doctrine): a line enters only on repeated logged
failure; a line leaves the day the gate superseding it ships. Each bullet stays traceable to
a defect-corpus entry. Bullets are independently deletable so single-bullet ablation is
possible.

## Why system-prompt position, and not the alternatives

- **Not CLAUDE.md** — first-user-message weight, the same injection tier as the memory
  entries that already failed against the verification-skip pathology. Global CLAUDE.md also
  rides in every session (project-c, project-b, …), paying a per-turn token tax and
  asserting close-consumer gates in contexts with no such pipeline.
- **Not memory entries** — same tier, already proven insufficient.
- **Not the pi-extensions package** — the file belongs with the enforcement code, not the TUI packaging.
- **Append, not replace** — preserves default tool guidance and safety instructions; we
  supply only what differs. Replacement is reserved for seats whose surface, identity, or
  permission model diverges from Claude Code's.

## Serving path

Repo is canonical; `~/.claude/` is a pointer. Git handles sync across hosts and the alias path
never changes regardless of checkout location.

```sh
ln -s /path/to/AgentRedisBridge/prompts/arb-close-discipline.md \
      ~/.claude/arb-close-discipline.md
```

Create this symlink in the same bootstrap that installs the pi extensions; checkout paths differ
per machine.

`claudeya` is a shell FUNCTION, not an alias — aliases do not expand inside other aliases in
scripts or non-interactive invocations, whereas functions compose and resolve paths reliably.

**Quiet-failure guard.** The deployment note names the failure mode: a dangling symlink
(repo moved, or not yet cloned) fails QUIETLY as an unreadable file, and the seat runs with
no close discipline while appearing normal. The shipped `claudeya` therefore tests the file
is readable and refuses to launch if it is not — a loud failure in place of a silent one.
This is an addition to the note's prescribed function body, for exactly the risk the note
identifies. The post-setup smoke test remains worth running on a new machine:

> ask the seat "what does your close discipline say about polished claims?" and confirm the
> block landed.

## Not yet done — the Pi-orchestrated route

`claudeya` covers CLI-invoked seats only. Pi-orchestrated warm turns BYPASS the shell
entirely, so the bridge harness (`arb-orch` wizard / `agent-dispatch`) must inject the same
file by direct repo path — which it can, since it already runs from the checkout. Until that
lands, the two routes are NOT byte-identical by construction: CLI seats carry the block and
Pi-orchestrated warm turns do not.

## Repo-name note — fixed in v2

Both artefacts v1 referred to the repo as "AgentBridgeRedis" / "agentbridgeredis". The actual
repository is **AgentRedisBridge**. This was not cosmetic: v1's symlink command pointed at a
path that does not exist, so following it verbatim produces a dangling link — and a dangling
link is precisely the quiet failure the note itself warns about, where a dangerous-perms seat
launches with no close discipline while looking normal. v1 would have bootstrapped its own
documented failure mode.

Corrected in both v2s, along with two other reference-vs-reality gaps: v1's `claudeya` body
omitted the readability guard that is actually deployed, and v1 asserted both serving routes
were "byte-identical by construction" when only the CLI route is wired.
