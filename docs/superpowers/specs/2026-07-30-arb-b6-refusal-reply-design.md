# ARB-B6 — Refusal reply envelope for parse-refused dispatches (design, first draft)

**Status:** DRAFT for four-seat review. Author: cold-Opus seat `cold-opus-author-b6`, no prior
context, implements nothing. **Scope:** design only; fleet deployment/restart of the seats is
owner-gated and explicitly out of scope for this note.

**SCOPE RULING 2026-07-30 (owner, Mark: "do B6 option A") — THIS DESIGN IS NOW v4, DE-SCOPED TO
THE CONFIRMED CORE.** Three review rounds (r1 four-seat, r2v and r3v finder) confirmed the guard
chain, the reply shape, the payload rules and the honest boundary prose, and refuted BOTH hardening
mechanisms as unreachable inside this slice:

- **send-once is not achievable with any expiring key** — the processing list has no TTL, so
  reliable-inbox recovery can re-present an envelope arbitrarily late (r3v, `redis_io.py:20`,
  `bridge.py:1230`);
- **no shared-inbox rotation scheme is both safe and bounded** — a dispatcher cannot prove reply
  ownership, so any cap can delete a live sibling's consume-once reply (r2v/r3v,
  `agent-dispatch:663`).

Both are pre-existing bridge-protocol limitations, not defects introduced here. **What ships:**
§2's corrected validation facts, §4's `kind: "reply"` shape and back-compat matrix, §5's zero-echo
fixed-token payload (including `structured: null` per `SPEC.md:314-319`), §6's five-guard chain
with the honest "local target-set restriction" wording, §7's unchanged dead-letter disposition,
§8 items 1–3 and 6–7, and acceptance criteria A1–A14 (A15/A16 are withdrawn with their mechanisms).

**What is NOT fixed, carried as named residuals (each becomes a backlog item at landing, per the
"no silent caps" rule):**

- **R-A: replayed/redelivered refused envelopes each produce a reply.** Bounded only by the same
  bus-write-access threat model as B6-F1(b). Structural fix requires either a TTL'd processing
  list or a durable idempotency horizon that outlives every recovery path.
- **R-B: orphaned replies (refusals included) rotate indefinitely** in sibling dispatchers'
  RPUSH-back loop, costing one redis round-trip per sighting. Structural fix is per-request reply
  keys — a protocol change across every dispatcher and seat, explicitly NOT smuggled into this
  slice.

Shipping the core is still a strict improvement on the filed defect: a refused dispatch returns a
crisp `error_code` at exit 1 in seconds instead of a silent 90-minute timeout indistinguishable
from a hung seat. The residuals make it *noisier under replay*, never less honest.

## 1. The defect

`handle_raw` refuses an unparseable envelope by logging and returning, with no reply:

```python
# src/agent_redis_bridge/bridge.py:1284-1289
def handle_raw(self, raw: str, *, processing_raw: str | None = None) -> bool:
    try:
        envelope = Envelope.from_json(raw)
    except EnvelopeError as exc:
        logger.error(f"[bridge-error] envelope-invalid {exc}")
        return False
```

On a `BRIDGE_TASK_REF_REQUIRED=1` seat a legacy string task raises `EnvelopeError(
"invalid-payload-task-ref")` (`src/agent_redis_bridge/envelope.py:44-46`; the ref-shaped-but-malformed
sibling path is `src/agent_redis_bridge/brief_ref.py:41-50`). The sender then blocks in the
dispatcher's `BLPOP` loop until `--timeout` expires and exits 124
(`scripts/agent-dispatch:663-665`, `:693`) — byte-for-byte the same experience as a hung seat.
Observed and recorded: `.claude/wave1-evidence/2026-07-29/REPORT.md:112-115`
("legacy callers experience silence-then-timeout, not a crisp error reply").

The interim mitigation this design retires is the ARB-B4(c) diagnosis entry plus the cron soak
scan (`.claude/wave1-evidence/2026-07-29/soak/wave1-soak-scan.sh:30`) — both route a human to a
launchd log the sender cannot read.

## 2. What is already validated when the refusal fires

`Envelope.from_json` validates in a fixed order:

| step | `envelope.py` | establishes |
|---|---|---|
| JSON object | `:65-71` | parseable, is a dict |
| required keys present | `:73-76` | `id from branch to kind sent_at payload` |
| header fields nonblank `str` | `:78-80` | **`id`, `from`, `branch`, `to`, `sent_at` usable** |
| `kind` in `ALLOWED_KINDS` | `:82-83` | `kind` is one of six known values |
| `payload` is a dict | `:85-86` | payload addressable |
| `in_reply_to` coherence | `:88-93` | — |
| **request payload block** | `:95-124` | *this is where the refusals below are raised* |
| `run_id` type | `:130-132` | — |

So every error raised at `:95-132` fires with the full reply-addressing set already proven:
`id` (correlation), `from` (recipient of the reply), `branch`, `to` (recipient check).
**Corrected in v2 (r1, all four seats; sol executed the probe):** allowlist membership does NOT
establish `kind == "request"` — `invalid-run_id` is raised at `:130-132`, *outside* the
kind-specific block, so a valid-header `steer`/`cancel`/`notify`/`reply` with a bad `run_id`
raises the same allowlisted reason (probe: all five kinds → `invalid-run_id`). The kind
invariant is therefore its own explicit guard in §6, never an inference from the reason token.
A reply is constructible for the validated-header set; it is *permitted* only for requests.

Repliable reason tokens — the closed allowlist (all literal, no input interpolation):

`invalid-payload-task-ref` · `missing-payload-task` · `invalid-payload-thread_id` ·
`invalid-payload-fork_from_thread_id` · `invalid-payload-claim_ref` · `invalid-payload-lane` ·
`contradictory-context` · `invalid-run_id`

Non-repliable by construction: `invalid-json: <msg>` (`:68`), `envelope-not-object` (`:71`),
`missing-<field>` / `invalid-<field>` (`:76`, `:80`), `invalid-kind:<kind>` (`:83`),
`invalid-payload` (`:86`), the `in_reply_to` pair (`:91`, `:93`), `missing-payload-message` (`:128`).
The first group has no addressable sender or no correlation id; the last two interpolate
attacker-controlled text into the reason string, which is the second reason to exclude them.

## 3. The ordinary reply path this must mirror

`Bridge.send_reply` (`bridge.py:3878-3907`) builds the payload, calls `make_reply`
(`envelope.py:171-188`), `LPUSH`es to the sender and logs `[reply-sent] <id> in_reply_to=<request id>`.
`self.redis.lpush(agent_id, body)` targets `inbox_key(agent_id)` (`redis_io.py:279-280`, `:81`) —
the **reply** lane. Milestones use `send_milestone` → `make_notify` and, under
`BRIDGE_NOTIFY_INBOX=0`, `notify_inbox_key` (`bridge.py:4487-4506`, `redis_io.py:98`). The refusal
reply MUST use the `send_reply` lane, never `send_milestone`: `agent-dispatch` drops `kind == "notify"`
outright (`scripts/agent-dispatch:682-684`), so a notify-lane refusal is invisible to the waiter
under either `BRIDGE_NOTIFY_INBOX` setting.

There is direct precedent for an *error* reply from inside `handle_raw` before any engine work:
turn-timeout refusal (`bridge.py:1310-1314`) and sender-rejected (`bridge.py:1316-1322`), both
`send_reply(..., TurnResult(ok=False, result="", error=...))`.

## 4. Fork 1 — envelope shape: `kind: "reply"` (RECOMMENDED) vs `kind: "refusal"`

**Recommend `kind: "reply"` with an error payload.** The dispatcher's filter is
`kind == "reply" && in_reply_to == ID` (`scripts/agent-dispatch:667-672`); a matching reply prints
`.payload`, reads `.payload.ok` and exits 0/1. A refusal reply therefore terminates the wait
immediately at exit **1** with the reason on stdout — **zero dispatcher change**, and every
already-deployed dispatcher on the fleet gains the behaviour the moment a seat is upgraded.

Reject `kind: "refusal"`: `ALLOWED_KINDS` (`envelope.py:13`) would need a new member, so an OLD
seat receiving one would raise `invalid-kind:refusal` and, worse, an old *dispatcher* would fall
through the reply branch to the "sibling dispatcher" `RPUSH`-back at `scripts/agent-dispatch:689`
— re-queuing the refusal forever and still exiting 124. The clean-taxonomy argument is real but it
buys a new kind at the cost of making the fix invisible to precisely the callers who need it.

**Back-compat matrix.** Old dispatcher + new seat: exit 1 with payload printed (works, this is the
point). New dispatcher + old seat: unchanged 124 timeout (no regression). Old seat + refusal reply
addressed to it: cannot occur — refusals only go to explicitly rostered senders (guard 5).

**Orphan rotation (v3 — r2v sol refuted v2's drop-after-25: a dispatcher CANNOT distinguish an
orphan from a live sibling's reply, and no fairness guarantee makes "the rightful sibling wins
within 25" an invariant — sol executed a legal schedule where the wrong waiter won 25 straight
and deleted the sibling's consume-once reply).** v1's "bounded by the number of concurrent
dispatchers" comment (`agent-dispatch:686-688`) remains FALSE for true orphans — they rotate
forever. v3 takes only NON-DESTRUCTIVE mitigations:
- (a) the §7 send-once guard caps refusal-orphan VOLUME at one per envelope id per horizon;
- (b) `agent-dispatch` hygiene: per-id sighting counter drives an escalating RPUSH-back backoff
  (sleep `min(sightings, 25) * 0.2s` before re-queueing a non-mine reply) and ONE
  `orphan-reply-suspected id=<id> sightings=25` stderr line for diagnosis. **No reply is ever
  dropped by a process that cannot prove ownership.** The spin COST is bounded; the litter
  itself is not — that requires an ownership-aware disposition (per-request reply keys), which
  is a protocol change filed as follow-up backlog, not smuggled into this slice.

**Distinguishability residual (P2, OPEN).** Exit 1 conflates "engine ran and failed" with "never
admitted". The payload distinguishes them (`refused` is present), but a shell caller reading only
`$?` cannot. Proposed follow-up, not part of this slice: an opt-in `--refusal-exit N` in
`agent-dispatch`. Adding a third exit code unconditionally would change a published contract
(0/1/124) and is deliberately not recommended here.

## 5. Fork 2 — what the refusal payload carries

**Recommend: fixed tokens only; nothing from the offending input is echoed.**

```json
{
  "result": "",
  "ok": false,
  "error": "envelope-invalid: invalid-payload-task-ref",
  "error_code": "invalid-payload-task-ref",
  "refused": "envelope-parse",
  "task_type": "str",
  "task_ref_required": true,
  "completion": null,
  "thread_id": null,
  "artifact_paths": []
}
```

- `error_code` is drawn from the §2 allowlist. If a future `EnvelopeError` reason is not in the
  allowlist, the seat MUST fall back to log-only silence rather than forward an unknown string —
  the allowlist is the sanitiser, not a size cap.
- `task_type` is the *Python type name* of `payload.task` mapped through a fixed vocabulary
  (`str|dict|list|int|float|bool|null|other`). This is the one fact a legacy caller needs ("you sent
  a string") and it cannot carry attacker bytes.
- `task_ref_required` echoes `task_ref_required()` (`envelope.py:24-26`) so the caller learns the
  seat's posture without reading the plist.
- **No echo of `payload`, `payload.task`, or the raw envelope, at any size.** A size-cap-and-truncate
  policy was considered and rejected: truncation still reflects attacker-chosen bytes into another
  agent's inbox and into its log, and the diagnostic value over `error_code` + `task_type` is nil.
- The `result/ok/error/completion/thread_id/artifact_paths` keys are present for shape parity with
  `send_reply` (`bridge.py:3888-3896`) so existing payload consumers do not need new key guards.
- **`structured` (v2 — OPEN-3 resolved by SPEC, r1 cold-Opus):** `SPEC.md:314-319` pins the
  contract — replies to `expect_structured: true` requests carry `structured`, with `null` as the
  parse-failure convention. The refused envelope's `payload` is already a validated dict when any
  allowlisted reason fires (`envelope.py:85-86` precedes the kind block), so the refusal includes
  `"structured": null` when `payload.get("expect_structured")` is truthy, and omits the key
  otherwise — exactly the SPEC's existing shape. One sentence lands in `SPEC.md` alongside the
  implementation stating that parse-refusal replies follow the same convention.
- Bounded by construction: every value is a literal or a fixed-vocabulary token, so the payload is
  ≤ 512 bytes regardless of input. This is an invariant to assert, not a runtime truncation.

## 6. Fork 3 — which refusal classes reply

Guard order (v2 — five guards; r1 added guard 2 and rewrote guard 5):

| # | condition | source of truth | on failure |
|---|---|---|---|
| 1 | reason in §2 allowlist (⇒ header valid) | `envelope.py:73-83`, `:95-132` | log-only, as today |
| 2 | `header.kind == "request"` — EXPLICIT, never inferred (r1 unanimous) | `envelope.py:82-83` proves kind is parsed; §2 correction proves it must be checked | log-only |
| 3 | `header.to == self.agent_id` | mirrors `bridge.py:1291-1293` | log-only |
| 4 | `header.from != self.agent_id` | mirrors `bridge.py:1295-1297` | log-only (self-reply loop) |
| 5 | `header.from` has an EXPLICIT sender-policy entry on this seat AND that policy `!= "reject"` (v2 — rewritten per r1 cold-Opus P1-2) | the seat's configured `--sender-policy` pairs; NOT `.get(sender, unknown_sender_policy)` | **log-only — the reflection-restriction case** |

**Guard 5 is a local target-set restriction, NOT a security boundary (v2 prose correction, r1 sol
P1-2 + cold-Opus P1-2/P1-3).** `from` is a self-declared string with no cryptographic binding (no
signature verification exists anywhere in `src/agent_redis_bridge/`), so nothing here
authenticates a sender; the actual boundary is bus write-access (a principal that can write one
inbox can generally write the victim's directly). What guard 5 does is bound where THIS path can
be aimed: only at agents the operator explicitly configured on this seat. The v1 form (`resolved
policy != reject`) was void on any seat running `--unknown-sender-policy human|trusted` (both are
supported deployments — `bridge.py:4726`, `:4761`): every forged `from` would resolve non-reject
and the seat became an arbitrary reflector with attacker-chosen `in_reply_to`. Requiring an
explicit roster entry makes the restriction hold regardless of the unknown-sender default.
`human` entries receive refusals alongside `trusted` — the criterion is "explicitly configured
relationship", not privilege tier.

**Per remaining class:**

- **`envelope-wrong-recipient` (`bridge.py:1291-1293`): NO reply — unchanged.** `to` is the field a
  misrouter or forger controls; replying would let any seat be used to reflect at a `to` value it
  does not own, and the guard fires before sender policy is ever resolved. The genuine misroute case
  (sender pushed to the wrong inbox key) is a bus-config defect, and its diagnosis belongs in
  `diagnose-live-dispatch`, not in a reflected reply. Revisit only with a signed-sender mechanism.
- **`sender-rejected` (`bridge.py:1316-1322`): UNCHANGED in this slice — but the claim is now
  honest and the decision is ESCALATED, not deferred (v2, r1 sol P1-2 + cold-Opus P1-3).** Two
  pre-existing paths reply to senders this design's guard 5 would silence: `sender-rejected`
  itself, and the turn-timeout refusal above it (`bridge.py:1310-1314`, reachable by rejected
  senders via `validate_requested_turn_timeout`, `bridge.py:1602-1603`). Sol EXECUTED the
  reflection: a forged `from` naming a victim makes the seat push correlated error envelopes into
  the victim's inbox on both paths, and `sender-rejected` echoes the forged `from` verbatim into
  its error string. So today, guard 5 removes no attacker capability — it only prevents THIS new
  path from widening the reflector. **Owner fork B6-F1 (decision required before this slice's
  claims can strengthen, not before it lands):** (a) align — resolve `reject` before the
  turn-timeout check and make rejected senders log-only on both existing paths (a reply-contract
  change: probes that today get "sender rejected" replies would get silence); or (b) accept and
  document the threat model — bus write-access is the trust boundary, inbox-writers are
  authorized principals — and track sender authentication as its own backlog item. This design
  works under either ruling; its own path is safe under both.
- **Header-invalid classes:** NO reply. No correlation id and/or no addressable sender (§2).

## 7. Fork 4 — dead-lettering: no change

**Recommend: disposition unchanged; the reply is additive visibility only.** Today a parked
refused envelope is acknowledged and discarded by the `finally` block at `bridge.py:1174-1178`
(`remove_processing`, because `handle_raw` returned `False` ⇒ `worker_owns_processing` false). There
is no dead-letter list in `redis_io.py` — `processing`/`processing_claim` are recovery machinery
(`redis_io.py:87-96`, `:352-397`), not a DLQ. A refused envelope is a *sender* defect the sender can
now see and fix; retaining bodies would create an unbounded store of attacker-supplied bytes for no
consumer. Adding a DLQ is a separable proposal with its own retention and access questions.

**Send-once guard (v3 — r2v sol refuted the v2 form: a 1-hour NX key is a rate limit, not
send-once, and an unscoped key lets one seat suppress another's legitimate refusal).** Refusals
fire before the duplicate-request check and the budget, and the wave-1 probes recorded double
refusal log lines via the redelivery path (`REPORT.md:114`), so without a guard a replayed or
redelivered refused envelope mints a reply every time. v3 mechanism:
`SET NX EX <events_ttl>` on the prefixed, SEAT-SCOPED key
`refusal_sent:<self.agent_id>:<envelope id>`; reply only when the SET wins.
- **Horizon = the seat's configured `events_ttl`** (default 7 days, `bridge.py:4695`) — chosen
  because that is the upper bound of the reliable-inbox redelivery machinery itself
  (`bridge.py:1191-1205`, recovery `:1230-1253`, claim TTL `:1197-1204`): every path that can
  legitimately re-present the same envelope id expires at or before this horizon.
- **The honest claim (stated, not overclaimed):** at most one refusal reply per envelope id per
  seat WITHIN the redelivery horizon. Beyond it, a re-presented id is a new conversation by the
  same rule the reliable inbox applies to everything else. Cumulative replay by an attacker is
  bounded to one reply per horizon per id — a rate statement, and the design says so.
- **Seat scope:** two rostered seats refusing the same (attacker-reused) id each reply once —
  correct, since each holds its own conversation with the sender; and neither can suppress the
  other (r2v's cross-seat suppression hole).
The OPEN-1 double-log mechanism still deserves an explanation in the impl report; its blast
radius is one reply per horizon regardless.

## 8. Implementation shape (for the reviewers to attack, not a plan)

1. `envelope.py`: extract the header validation of `:65-93` into `parse_header(raw) -> EnvelopeHeader`
   (frozen dataclass: `id from branch to kind sent_at payload`), called by `from_json` so there is one
   parser. Wrap the `kind`-specific block `:95-132` so an `EnvelopeError` raised there is re-raised
   with `exc.header` set; `exc.header` stays `None` for every earlier failure. This makes "was the
   header valid" a fact the caller reads, not a re-parse it guesses.
2. `bridge.py`: in the `except EnvelopeError` at `:1287-1289`, keep the existing log line **byte-identical**
   (the cron soak scan greps `invalid-payload-task-ref` and the `[inbox]` line above it —
   `wave1-soak-scan.sh:30`, `:39`), then call a new `send_refusal_reply(header, reason)` when the four
   §6 guards pass. Use `make_reply` directly rather than `send_reply`: `send_reply` takes an
   `Envelope` and applies `timeout_echo_fields`/`expects_structured` (`bridge.py:3888-3898`), and
   fabricating an `Envelope` that `from_json` would refuse to produce is exactly the kind of
   invalid-state object that leaks later.
3. Log `[reply-sent] <reply id> in_reply_to=<request id>` from the refusal path too, matching
   `bridge.py:3907`, so one grep covers both lanes.
4. Send-once guard per §7 (v3): `SET NX EX <events_ttl>` on the prefixed, seat-scoped
   `refusal_sent:<self.agent_id>:<envelope id>` key; reply only on SET-wins.
5. `scripts/agent-dispatch` (v3 — amends v1's "no change"; v2's destructive drop is WITHDRAWN):
   per-id sighting counter drives escalating backoff before RPUSH-back plus one diagnostic
   stderr line at 25 sightings. Never drops. No change to the reply filter or exit-code contract.
6. Guard 5 reads the seat's EXPLICIT sender-policy map only — the implementation must not consult
   `unknown_sender_policy` on this path (see §6; the two lookups must not be conflated in code).
7. **Posture-injection seam (v3 — r2v sol §6):** `Bridge` captures the task-ref posture ONCE at
   construction (explicit constructor/args value, falling back to the env read at startup only)
   and passes it as `ref_required=` into `Envelope.from_json` at the `handle_raw` call
   (`bridge.py:1286`; the parser already exposes the parameter, `envelope.py:64`). Handling-time
   ambient `os.environ` reads are removed from this path. A1/A11 set opposite postures through
   `make_bridge` via this seam; `FakeRedis` gains an NX+TTL-modelling `set` primitive for A15
   (`tests/test_bridge_handle_raw.py:14-46` currently cannot model NX).

## 9. Acceptance criteria

Unit tests use the existing harness (`tests/test_bridge_handle_raw.py:1027-1067` — `make_bridge`,
`request_json`, `FakeRedis.replies`), which is a list of `(agent_id, body)` and so supports exact
assertions on count, recipient and parsed body.

| # | check | asserted value | fails if |
|---|---|---|---|
| A1 | ref-required seat, `payload.task = "legacy string"` | `len(fake.replies) == 1`; `body["kind"] == "reply"`; `body["in_reply_to"] == "req-b6"`; `body["to"] == "claude-project-c-dev"`; `payload["ok"] is False`; `payload["error_code"] == "invalid-payload-task-ref"`; `payload["task_type"] == "str"` | reply absent, mis-addressed, uncorrelated, or code drifts |
| A2 | same, log assertion | the record `"[bridge-error] envelope-invalid invalid-payload-task-ref"` still emitted, unchanged | someone "tidies" the line the soak scan greps |
| A3 | malformed ref `{"artefact_id": "art-x", "version": 0}` | `error_code == "invalid-payload-task-ref"`, `task_type == "dict"` | the ref-shaped path (`brief_ref.py:49-50`) is missed |
| A4 | **security case** — same refusal, `from = "nobody-agent"`, `--unknown-sender-policy reject` | `fake.replies == []` **and** the `envelope-invalid` log record present | guard 4 removed ⇒ this goes to 1 reply. Asserting the log line too prevents a vacuous pass where the envelope was rejected earlier for an unrelated reason |
| A5 | `to = "some-other-seat"`, ref-required, legacy task | `fake.replies == []` | wrong-recipient starts reflecting |
| A6 | `from = <seat's own agent_id>` | `fake.replies == []` | self-reply loop |
| A7 | header-invalid: `{"kind":"request"}` missing `id` | `fake.replies == []`; log reason `missing-id` | reply attempted without a correlation id |
| A8 | non-repliable reason with a valid header (`missing-payload-message`, `kind = "steer"`) | `fake.replies == []` | allowlist widened by accident |
| A9 | payload bound | `len(json.dumps(payload)) <= 512` with `payload.task` set to a 1 MiB string; and `"legacy" not in json.dumps(payload)` for `task = "legacy-secret-xyz"` | any echo of input is reintroduced |
| A10 | **negative control** — ordinary valid `request` on the same seat | reply payload equal to the pre-change build's for the same input after masking the generated fields (`id`, `sent_at`) — structural equality, not byte-golden (v2: `make_reply` mints uuid4/iso_now, so byte-identity is unattainable); `len(fake.replies) == 1` | the refusal path perturbs the ordinary path |
| A11 | **negative control** — dual-accept seat (`BRIDGE_TASK_REF_REQUIRED` unset), legacy string task | request is *admitted*: `fake.replies` carries an ordinary reply, no `error_code` key | the change silently refuses on dual-accept seats |
| A12 | end-to-end against a live seat: `agent-dispatch --timeout 30` with a legacy string | exit status **1** within **< 10 s**, stdout JSON has `.error_code == "invalid-payload-task-ref"` | still 124/timeout ⇒ the whole premise unfixed. Assert the duration, not just the code: exit 1 at t=30 s is a different bug |
| A13 | **kind guard (new in v2, r1 unanimous)** — parameterized over `reply`, `steer`, `cancel`, `notify`, each with a valid header and invalid `run_id`, delivered on BOTH the ordinary path and (for `steer`/`cancel`) the control lane (`bridge.py:1115`) | `fake.replies == []` for every case; log records `invalid-run_id` | guard 2 removed or inferred-from-reason again — sol's executed probe shape |
| A14 | **roster guard (new in v2, cold-Opus P1-2)** — seat configured `--unknown-sender-policy human`, refusal-eligible envelope with unconfigured forged `from` | `fake.replies == []` | guard 5 falls back to the unknown-sender default and the seat reflects |
| ~~A15~~ | **WITHDRAWN v4** (owner option A) — the mechanism it pinned is unreachable; residual R-A. Original text: send-once within horizon (v3) — the SAME refused envelope delivered twice (simulated redelivery); then TTL-expiry modelled and a third delivery; then the same id refused on a SECOND seat | deliveries 1–2: exactly one reply; post-expiry delivery: one more reply (the honest rate claim, asserted, not hidden); second seat: replies independently (key is seat-scoped) | unbounded replay replies; or cross-seat suppression; or the test hides the expiry semantics |
| ~~A16~~ | **WITHDRAWN v4** (owner option A) — the mechanism it pinned is unreachable; residual R-B. Original text: orphan backoff, non-destructive (v3) — TWO concurrent waiters on one `FROM` inbox plus one synthetic orphan refusal reply | each real reply reaches its owner (both waiters exit on their own ids — the safety property); the orphan is never dropped; per-sighting backoff observed rising; one `orphan-reply-suspected` stderr line at 25 | a live sibling's reply is destroyed, or the spin-cost mitigation silently became a drop |

Harness self-check before any of the above is believed: run A1 against the **unmodified** seat and
confirm it FAILS with zero replies. A suite that passes A1 pre-change is not testing this change.
Existing regression floor: `tests/test_envelope_dual_accept.py:85-109` must stay green unmodified —
those assert the exact reason tokens the refusal payload now transports.
Env-coupling note (v2, cold-Opus P2): A1/A3/A11 must construct the seat's ref-required posture
in-process (constructor/args), never via ambient `BRIDGE_TASK_REF_REQUIRED` in the test
environment — an env-coupled test is green or red depending on the shell that runs it.

## 10. OPEN — v2 dispositions (r1-adjudicated)

- **OPEN-1 — CONTAINED, mechanism still owed.** The double-refusal-log mechanism (`REPORT.md:114`)
  remains unexplained, but the §7 send-once guard bounds its consequence to one reply. The impl
  report must state the mechanism once reproduced (or record that it no longer reproduces).
- **OPEN-2 — RESOLVED**: send-once guard (§7).
- **OPEN-3 — RESOLVED by SPEC** (`SPEC.md:314-319`): refusal carries `structured: null` when the
  refused payload requested it (§5), plus a one-sentence SPEC addition in the landing commit.
- **OPEN-4 — RESOLVED by guard 2**: non-request kinds never elicit a reply, on either lane; pinned
  by A13's control-lane cases.
- **OPEN-5 — RESOLVED empirically**: `scripts/check-doc-index` at dev HEAD does not flag the
  committed 2026-07-30 spec files (orchestrator ran it 2026-07-30; only the 16 ARB-B20 files are
  missing), so no index entry is required by the current gate. If `doc_index_lib` later widens to
  this directory, that lands with its own change.

## 11. Owner fork registry (this design)

- **B6-F1** (§6, escalated by r1 sol P1-2 / cold-Opus P1-3): align the two pre-existing
  rejected-sender reply paths to log-only, or accept-and-document bus-write-access as the trust
  boundary. This slice is safe under either; the ruling changes other paths' contracts, not this
  one's.

## 12. v2 changelog (fold of panel r1 — run `panel-b6-design-r1-20260730T031643Z-baa1f3`, closed
needs-changes; votes: sol block/P1, grok block/P1, cold-Opus block/P1, agy needs-changes/P1)

- §2 corrected: allowlist membership does NOT imply `kind == "request"` (`invalid-run_id` raised
  outside the kind block; sol's five-kind probe). Guard 2 added; A13 pins it on both lanes.
- Guard 5 rewritten to explicit roster membership — the v1 `resolved policy != reject` form was
  void under `--unknown-sender-policy human|trusted` (cold-Opus P1-2); A14 pins it.
- Guard prose downgraded from "amplification boundary" to "local target-set restriction"; the
  pre-existing rejected-sender reflector is now stated plainly and escalated as owner fork B6-F1
  (sol P1-2 executed the reflection; silently deferring while overclaiming was ruled out).
- Send-once guard added (`refusal_sent:<id>`, SET NX EX 3600) — resolves OPEN-2, contains OPEN-1,
  bounds replay amplification; A15 pins it.
- Orphan-rotation claim corrected (the "bounded" comment at `agent-dispatch:686-688` is false for
  orphans); in-slice dispatcher hygiene cap added (drop after 25 sightings); A16 pins it
  (cold-Opus P1-4).
- `structured: null` on refusals when requested, per `SPEC.md:314-319` (OPEN-3), with a
  one-sentence SPEC addition at landing.
- A10 made implementable (structural equality masking generated fields); A1/A3/A11 de-env-coupled
  (cold-Opus P2s); grok's "interpolation" prose nit noted for §2's non-repliable rationale.

## 13. v3 changelog (fold of r2v — run `panel-b6-r2v-20260730T033123Z-103d6f`, sol block/P1;
folds 1–3 confirmed, folds 4–5 refuted and re-designed, fold 6 seam named)

- Send-once re-specified: seat-scoped key `refusal_sent:<agent_id>:<envelope id>`, horizon =
  `events_ttl` (matches the reliable-inbox redelivery machinery's own upper bound), and the claim
  downgraded to the honest form — once per id per seat per horizon (sol proved `EX 3600` was a
  rate limit wearing a send-once label, and the unscoped key allowed cross-seat suppression).
- v2's drop-after-25 orphan cap WITHDRAWN — sol executed a legal schedule where it deleted a live
  sibling's consume-once reply. Replaced with non-destructive escalating backoff + one diagnostic
  line; ownership-aware disposition (per-request reply keys) filed as follow-up backlog, not
  smuggled in.
- Posture-injection seam specified (Bridge captures ref-required posture at construction, passes
  `ref_required=` to the parser at `handle_raw`; no handling-time env reads) — makes A1/A11
  implementable as written; FakeRedis NX+TTL primitive named for A15.
- A15/A16 rewritten to pin the surviving properties, including the expiry semantics stated
  honestly and the two-waiter safety property.
- A14 note: parameterize over both `--unknown-sender-policy human` and `trusted` (r2v §2).

## 14. v4 changelog (owner scope ruling, 2026-07-30 — "do B6 option A")

- De-scoped to the confirmed core; §7's send-once guard and §4/§8's orphan mitigation are
  WITHDRAWN as mechanisms, with A15/A16 marked withdrawn in place (not deleted — the refutations
  are the record of why an expiring key cannot carry a send-once claim here).
- Residuals R-A (replay produces a reply each time) and R-B (orphan rotation) named in the scope
  banner; both file as backlog items in the landing commit.
- §8 implementation list reduces to items 1–3 (header extraction, guarded `send_refusal_reply`,
  `[reply-sent]` log parity) plus 6 (guard 5 reads the explicit roster only) and 7 (posture
  injection seam — the one fold r3v CONFIRMED implementable).
- Owner fork B6-F1 (the pre-existing rejected-sender reflector) stays open and independent; this
  slice is safe under either ruling and does not widen the reflector.
