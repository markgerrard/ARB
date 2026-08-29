# Orchestrating multiple Claude peers — field notes from the Project A ↔ Project B run

[claude-peer-coordination.md](claude-peer-coordination.md) covers the **plumbing** for two
Claude Code sessions coordinating over the bus. This doc covers the **workflow layer** for
the N ≥ 3 case: several interactive Claude sessions on different hosts collaborating on one
cross-host change, with one session acting as orchestrator. No engine, no daemon — every
participant is a human-attended Claude Code session in peer mode.

**Provenance.** `claude-project-a-1` (Project A's app server) orchestrating
`claude-project-b-1` (Project B's primary node) and `claude-project-b-a` (a second Project B
node, on separate infrastructure) through a bidirectional event-contract hardening and an
active-active cutover. The run used the v0 bash predecessor of this repo's watcher/send
scripts (`tools/agent-comms/` in the Project A repo — same inbox/heartbeat keys, same bus
shape) and remains the cleanest multi-peer use of the pattern to date. Everything below was
load-bearing in that run; none of it is speculative.

Related: [orchestrator-patterns.md](orchestrator-patterns.md) is the engine-dispatch
analogue (parallel Codex tasks). The difference here: peers are *autonomous sessions that
negotiate*, not workers you dispatch to — which changes what the orchestrator must pin down
in writing.

---

## 1. Designate a coordination lead, in writing

With two peers, coordination is symmetric chat. From three up, work spanning multiple hosts
diverges fast — each session makes locally-reasonable decisions (priorities, sequencing,
doc conventions) that don't compose. Fix it structurally:

- **One agent is the lead** — normally the session on the repo where most of the work lands.
- The lead **sequences cross-host work and assigns slices**; peers execute and report.
- Peers and lead communicate freely for tactical decisions **without round-tripping through
  the human**. The human stays final on product/scope only.
- The escalation path is explicit: peer disagreement → lead has final tactical call → human
  has final product call.
- The reciprocal rule for non-lead peers: **execute the lead's decisions, don't re-litigate.**
  Dissent is one flag with evidence, then execute (or escalate if it's genuinely
  product-shaped). Without this rule, agents bottleneck on the human asking "are we sure?"
  about things the lead already decided.
- **Record the designation as an ADR** in the lead's repo. On this run it mattered
  twice: it ended a sequencing disagreement instantly, and a later successor session
  re-derived its own role from the ADR (see §5).

## 2. Phase plans with observable gates are the shared ground truth

Peers cannot see each other's terminals. Chat history on the bus has no retention guarantee.
The only durable shared state is a **plan document in git**, owned by the lead:

- Numbered phases, each with a **concrete observable check** — not "queue works" but
  "`LLEN project-a_to_project-b:events:node-a` grows on publish, then drains; ack lands in
  `sync_event_log` within 60s".
- Peers report completion **with evidence**: event IDs, row IDs, log lines, commit SHAs.
  Prose without an artefact doesn't close a gate.
- The lead updates checkbox status; the plan file's git history is the run's audit trail.

This run's three-phase parity bring-up (publisher fanout roundtrip → new node as event
source → bidirectional failover) closed all phases in one day under this shape, including
phases where one peer deliberately broke its own host (stopping a consumer) while the other
peer and the lead verified coverage from their vantage points — three sessions, three hosts,
one synchronised test, coordinated entirely over the bus.

## 3. Verify independently before closing a phase

The repo-wide rule (*the reply is a claim; the commit is the evidence*) holds even more
strongly for peers, because a peer's "done" is filtered through what's observable from
*their* host. The lead re-verifies from its own side — queue depths, DB rows, log output —
before ticking the box.

On this run that caught **two real bugs in phases a peer had reported working**: an
error-classifier gap on data-shape SQLSTATEs, and an envelope field that wasn't propagated
into stored rows. Both were invisible from the emitting side and obvious from the consuming
side. Phase-close discipline is not bureaucracy; it's where cross-host bugs surface.

## 4. Decisions go in git, not the channel

Anything architectural, contract-affecting, or convention-setting gets an ADR
(`docs/decisions/YYYY-MM-DD-<slug>.md`, Context / Decision / Consequences) in the repo it
originates from, immediately, while the message thread is fresh. Two reasons proven in the
field:

1. **The bus is ephemeral.** Twice on this run, the difference between context lost and
   context recovered was that a decision had been written to git before the session ended.
2. **Cross-repo work needs a canonical side.** Shared contract documents live canonically in
   one repo; other repos hold mirrors that name the canonical source. The lead owns the
   canonical copy — but not authorship: the most consequential contract patch of the run was
   drafted by a *peer* post-incident and adopted into the canonical doc.

Index ADRs from the contract doc (one line each) so a reader of the contract finds the
reasoning without trawling `docs/decisions/`.

## 5. Sessions die; agent identities persist — design for succession

A Claude session is mortal; the agent_id is not. The succession mechanism, exercised on
this run:

- A successor session **claims the same agent_id** → it inherits any queued inbox messages
  (lists persist; nothing is lost while the seat was empty).
- It **reads the decision log + phase plan** → it inherits role and context without
  re-deriving them from scratch.
- Corollary: keep **one active session per agent_id**. Two sessions BLPOPping the same inbox
  race atomically — each message goes to exactly one of them and neither knows the other
  exists. Spin up `claude-<role>-2` instead.

This is also why §1 and §4 insist on writing things down: the ADR + plan + contract are what
make an agent seat *resumable* rather than tied to one session's context window.

## 6. The human can relay through any node

The human doesn't need to be at the lead's keyboard. On this run, the human's designation
of the coordination lead reached `claude-project-a-1` relayed *via* a `claude-project-b-1`
envelope. Any session the human happens to be talking to can carry instructions into the
channel — tag relayed-human content as such in the payload so the receiver can distinguish
"peer opinion" from "human instruction".

## 7. Shipping files between hosts with no shared filesystem

Peers on different hosts routinely need whole artefacts — a contract draft, a diff, a
schema. This run used a `file_drop` envelope carrying `{name, sha256, bytes, lines,
content}` plus a freeform `note`; the receiver verifies the sha before saving or processing.

Under this repo's envelope schema, express it as `kind=notify` with
`payload: {"event": "file_drop", "data": {name, sha256, bytes, lines, content, note}}`
(the v0 scripts had `file_drop` as a first-class kind; `notify` is the schema-valid home for
it here). Practical limits: envelopes are single list values — fine for docs and diffs (this
run shipped multi-hundred-line contract drafts), wrong for binaries or anything you'd
rather `scp`. Always include the sha; a truncated paste that still parses is the failure
mode you're defending against.

## 8. Monitor / wakeup operational experience

Adding to the gotchas in [claude-peer-coordination.md](claude-peer-coordination.md), from
sustained multi-day operation:

- **Arm the watcher at session start, not first use.** The watcher doubles as your
  heartbeat; peers treat a cold `:status` key as "offline" and won't wait on you. (Same
  conclusion as that doc's "Heartbeat without a daemon" §, reached independently.)
- **Run a re-armed wakeup safety net alongside the Monitor.** A `ScheduleWakeup` (~1800s,
  re-armed on every wake that doesn't end the loop) catches the case where the Monitor
  subprocess or the Redis connection dies *silently* — the failure mode where you'd
  otherwise sleep through peer messages indefinitely. The Monitor is the wake source; the
  scheduled wakeup is the dead-man's switch.
- **Truncated notification ≠ lost message.** The full envelope is always recoverable — from
  the Monitor task's output file, or (better) from the split watcher's per-id JSON on disk.
  Decide *up front* which recovery path your run uses and note it in the plan doc, so a peer
  saying "see message `abc123`" is actionable.
- **Check liveness before diagnosing silence.** A peer that hasn't replied is either
  thinking, dead, or never received the message. `TTL :status` distinguishes the first two;
  `LLEN` of *their* inbox distinguishes the third (sustained > 5 means they aren't
  draining). Do this before re-sending — duplicate envelopes are noise the receiver has to
  dedupe by id.

## 9. Reuse checklist (per run)

- [ ] All peers on the bus, watchers armed, heartbeats visible to each other
- [ ] Lead designated + ADR'd; escalation path stated
- [ ] Phase plan written with observable gates, before fan-out
- [ ] Evidence convention agreed (what closes a gate: IDs, SHAs, log lines)
- [ ] Cross-repo contract: canonical side picked, mirror convention stated
- [ ] Truncation recovery path chosen (split watcher dir or task-output file); file_drop
      with sha256 is the default for any payload over ~1KB
- [ ] Same-repo peers: ONE implementer assigned per change; others take verification roles
- [ ] Before any re-emit/replay: every claimant on new code, or its consumer paused
- [ ] Safety-net wakeups armed on every session

## 10. Empirical: a multi-week peer absence

§§ 5 and 8 are written from theory + design-time intent. This run exercised them under an
unplanned month-plus gap that's worth recording as a separate data point: nothing about the
channel was rebuilt to survive it, and yet it survived.

**Setup.** Three peers were active during the parity bring-up and a cross-host bug
propagation incident. Some time later, `claude-project-a-1`'s heartbeat key vanished from
the coordination DB. Reason unknown to the surviving peers — at the channel level the
session had simply ended.

**During the gap.** The two Project B peers stayed armed, each running a persistent Monitor
on the v0 watcher (`persistent: true`) plus a re-armed `ScheduleWakeup` as the dead-man's
switch for a silently-dead Monitor. Each wake checked Monitor liveness, its own heartbeat
TTL, its own inbox depth, and peers' heartbeats, then re-armed. The wakeup loop's own
runtime gate eventually expired on its own schedule; the Monitor was unaffected and kept the
channel armed the whole time — the safety net is for noticing a silently dead Monitor, not
for keeping the Monitor alive.

**Sustained-uptime numbers observed at reconnect:** both Project B peers' Monitors had run
continuously for over five weeks with heartbeat TTL oscillating in a narrow band, refreshed
on schedule the whole time, no drift, and empty inboxes throughout (nothing arrived to
drain).

**Reconnect.** Weeks later, a `kind: hello` envelope from `claude-project-a-1` landed
announcing it was back online (new session, same seat, coordination lead per the standing
ADR), context refreshed from the decision log and this repo's docs, no active tasking yet.
Monitor caught it cleanly, surfaced the truncated body as a notification, the full envelope
was recovered from the task-output file, and the surviving peers acked within about 20
seconds with a heartbeat snapshot. Channel operational from one envelope to the next.

**What this run promotes from theory to load-bearing:**

1. **Persistent Monitor survives weeks of idle, not just multi-day.** The §8 caveat "from
   sustained multi-day operation" is conservative — an earlier truncation fix in the split
   watcher script was sized for roughly a day and a half of continuous operation; this run
   held for five-plus weeks on the predecessor without recurrence. The split-watcher fix is
   still the right answer for any new run (truncation is a real regression mode and gets
   worse with payload size), but channel *liveness* isn't the failure shape that pushes the
   duration limit.
2. **Peer-watcher absence ≠ peer-application outage.** During the gap the orchestrating
   project's own application was healthy throughout — events published normally on the bus
   the entire time; only the Claude session monitoring the channel was gone. The
   §"Liveness" bullet in [§8](#8-monitor--wakeup-operational-experience) says "check
   liveness before diagnosing silence"; the stronger rule that falls out of this run is:
   **refuse to escalate a missing heartbeat as an application incident**. The heartbeat key
   is a *channel-level* reachability probe, full stop. App health lives elsewhere
   (production monitoring, queue depth in the app's own observability surface).
3. **Successor seats inherit role from the ADR + decision log + this repo's docs**, not from
   chat history. The reconnect message's context-refreshed framing was operative: the
   successor session had zero shared history with either surviving peer, and didn't need
   any. The designated-coordination-lead ADR (§1) and these workflow docs were sufficient to
   step back into the seat cold.
4. **The wakeup-loop ending is not a channel failure.** The dynamic runtime gate exists
   because indefinite background work isn't a reasonable default; when it expires, the
   surviving Monitor is what the channel actually rides on. Worth saying explicitly in the
   operating story so future operators don't escalate the loop's graceful exit.
5. **The lead's absence is recoverable; the lead's *decision log* being incomplete is not.**
   This is the stronger restatement of §4. The successor session was able to take over
   because the lead's repo held the ADR + plan + contract + decision history. If those had
   been thinner, the seat would have been resumable but the *role* wouldn't have been.
   Write to git proactively, not at end-of-run.

**Operating note** if you're armed for a long run and a peer goes silent: the right reflex is
not to escalate. It's:

```
EXISTS agent_scratch:agent:<peer>:status   →  0 = channel-offline
LLEN   agent_scratch:agent:<peer>:inbox    →  depth before you stop sending
GET    agent_scratch:agent:<peer>:status   →  who was last seen there (pid)
```

Then surface the absence to the human with a one-line factual note and
a recommendation (wait, proceed without, or page out-of-band).
**Re-sending the same envelope into a dead inbox is noise the
successor session has to dedupe**; queued messages persist, so a
single send is sufficient.
## 11. Round 2 — field notes from an incident-response + provisioning run

A second sustained run (rogue-consumer incident response overnight, then a live
provisioning workstream with two implementing nodes the next morning) confirmed
the rules above and produced these additions:

### The verifier needs the freshest briefing, not the implementer

Mid-run, the implementing node got every update promptly while the *verifying*
node fell one decision behind — and a verifier checking against stale
expectations false-fails parity. The human caught it ("has the secondary node been
notified?"). Rule: when a decision changes what done-looks-like (a handler now
*removes* a behaviour, a field moved into another blob), brief the verification
seat first, not last.

### Same-repo worker pools: one writer, explicitly assigned

Two peers deployed from one repo (two nodes of the same service) must not both
implement — duplicate commits race the shared main. The fix is procedural, not
technical: the lead assigns ONE implementer per change (the peer with the
tighter feedback loop or warmer tree); other nodes take verification /
parity-pull roles. The peers themselves flagged this before it bit — a peer
saying "don't give us both this work" is the convention working.

### Staged deploys race shared claim-stores — upgrade every claimant before re-firing

When peers share a dedup/claim database and consume fan-out delivery, replay
traffic intended to exercise NEW code can be claimed by a not-yet-upgraded
peer and processed with the old bugs — the exact bugs the replay was meant to
fix. (Here: the human caught it before fire.) Rule: before re-emitting, every
claimant is either on new code or has its consumer paused. The symmetric
upgrade is also the better test — both consumers exercised, parity ack becomes
direct evidence instead of inference.

### Multi-part plain messages can DROP parts, not just truncate

A 3-part plain re-send lost part 2/3 entirely (notification batching), and the
surviving parts still truncated on a raw watcher. The escalation ladder
terminates correctly at **sha-verified file_drop** — for anything over ~1KB,
skip the multi-part stage and go straight to file_drop. The full ladder, each
rung exercised for real: raw watcher → split watcher → file_drop with sha256.

### Written decision rights enable self-correction

A peer escalated a tactical call to the human, then *retracted its own
escalation* citing the lead ADR ("auth-mapping is tactical, your call —
owning that"). Conventions in git don't just settle disputes; agents
re-calibrate against them mid-run without the lead intervening.

### Codified evidence shapes propagate on their own

Peers began citing the evidence-pack conventions from this doc unprompted
("it's load-bearing because §2/§3 told me to do it that way") within one run
of the doc landing in their clones. Writing the convention down once beats
repeating it per dispatch.

### The orchestrator seat should stay thin

When the lead is also doing heavy local work, channel events queue behind it.
Delegate local chores (commits, doc edits, batch verification) to background
agents and keep the orchestrating session free to relay, sequence, and
verify — the human explicitly directed this and the run got faster.

### Humans are roaming approvers — relay verbatim and promptly

Push approvals and race-catches arrived via whichever session the human was
looking at, and needed relaying to the seats that act on them. Treat human
input as an event to route, with attribution (e.g. "operator signals go",
"operator's catch"), not context to absorb silently.

## Field notes — a Project A ↔ Project B node-side run

A two-party (lead + one peer) run: the Project A dev-box session led a Project B peer
through implementing two live-request-handling REST endpoints against a contract doc
authored (and panel-converged) on the Project A side. New lessons beyond the N ≥ 3 run:

### Back-pressure before build is the whole point of shape 2

The peer's phase-1 response to the contract file_drop was a **scope-changing
discovery** the lead could not have made: outbound requests resolve to live sessions
via an existing session variable, but inbound requests exist only inside the node's
listener process — inbound request handling needs new persistent infrastructure. An
engine dispatch would have either mis-built or stalled; the peer *negotiated*, with a
costed recommendation. Budget a real decision at phase 1, not a rubber stamp.

### The lead answers scope questions from the settled design, not from preference

The inbound-vs-outbound decision was resolvable without the human because the
converged design already encoded the answer (the SDK's origination story favored one
verb; the alternate verb was already deferred to v2). Pattern: **decide against the
written design, unblock the peer immediately, flag the decision to the human for
veto** — three sentences in the next human-visible message. Blocking the peer on a
human round-trip for a decision the docs already answer wastes the peer's session.

### Peer scope questions are interrupts, not queue items

The peer explicitly held the build ("no build until you answer, since it changes the
shape"). Once the notification plumbing was fixed (see the coordination gotchas in
claude-peer-coordination.md), the answer went back in minutes. Before the fix, the
question sat unread for 40 minutes — with a peer session idling the whole time. Lead
inbox latency is peer wall-clock.

### file_drop + sha256 echo is cheap and load-bearing

The peer staged the dropped contract into its own repo, verified the sha byte-for-byte,
and echoed it in the phase-1 gate. When the artifact IS the work order, that echo is
the difference between "implements the contract" and "implements a paraphrase of it."

### Expect the peer to surface adjacent findings — route them, don't absorb them

The peer flagged a pre-existing security hole in the middleware it was told to reuse
(fail-open on empty key + non-constant-time compare) on a surface that now terminates
live requests. Right handling: endorse, decouple (separate PR), and route to the human
with attribution — don't fold it into the current build's scope and don't silently
drop it.

## See also

- [claude-peer-coordination.md](claude-peer-coordination.md) — plumbing, day-1 setup, wire-level gotchas
- [orchestrator-patterns.md](orchestrator-patterns.md) — the engine-dispatch analogue
- [pipeline-operating-manual.md](pipeline-operating-manual.md) — workflow shapes for engine-based runs
