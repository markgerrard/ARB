# ARB Secrets — bus-native peer-to-peer secret transfer (design)

**Date:** 2026-07-02 · **Revision:** 2 (post spec-panel) · **Status:** design, pending implementation plan
· **Origin:** design panel + spec-review panel (`[ARB_RUN:arb-secrets-*-panel]`, 4 decorrelated seats
each) + operator decisions.

> **Revision 2 changes** (spec panel, all findings addressed): registry resolved to **pure bus-native
> TOFU with ARB Secrets' own pubkey store — no Postgres, ARB Messages' table untouched** (was an
> incoherent bus/Postgres split); **all** secret bodies (push *and* request/reply) now travel through
> the pointer→TTL'd-key→`GETDEL` indirection so ciphertext never rides inline into any retained store;
> §9 replaced "redact the tee" with a **structural no-secret-reaches-retained-store guarantee** + a
> verification matrix over the *actual* stores; reply now binds the **requested label** (wrong-secret-
> under-right-req_id fix); minimal **status key** added (failure visibility); rotation order fixed;
> replay state machine + seen-nonce TTL specified; `arb_crypto` extract given an explicit
> module-attribute-identity compat contract.

## 1. Purpose & scope

A 5th ARB capability: let two **Claude peers** (interactive sessions coordinating over the bus,
shape-2 in `docs/orchestrating-claude-peers.md`) hand a credential or env fragment to each other
**confidentially**, bus-native, without the value resting in plaintext anywhere a human-eyes tee, an
inbox dump, the split-watcher files, or `redis-cli LRANGE` can read it.

**Parties:** Claude peer ↔ Claude peer only. NOT the engine-dispatch panel; headless engines are out
of scope. Both endpoints can run Bash/PyNaCl, hold a long-lived keypair in a dotfile, verify payloads.

**Directions (both):** **A — push** (holder sends a cred to a specific peer); **D — request** (peer
asks a holder for a cred it needs). Driver: project-a-style multi-node runs (coordination-lead ↔
worker-node peers, cross-host over the shared managed bus). Either side can be the one lacking a cred the
other holds — which is why D is in scope.

**Non-goals (deferred, productization-era):** headless-engine credential custody; automated rotation
schedules; CRL/revocation propagation; protecting the decrypted value once in the recipient's process
heap or written to an env file (documented boundary, §9, not enforced). **A minimal delivery-status
key IS in scope** (§7) — the earlier "receipts deferred" non-goal is removed; it contradicted the
failure-visibility requirement.

## 2. Threat model

Per `arb-threat-model-recalibration`: threat is **mistakes, not a malicious orchestrator**, on trusted
solo infra. Modeled failures: a wrong/stale/test sender's payload mistaken for the trusted source; a
misaddressed secret; a plaintext credential leaking into a **retained** store; a single leaked dotfile
exposing more than it should.

**What sealing buys:** the managed bus is TLS+ACL, so the wire is protected. Sealing protects the
**at-rest copies** the wire encryption doesn't — the persisted tee, inbox dumps, split-watcher files,
`LRANGE`. This is load-bearing. The design makes it *true* structurally (§6 indirection + §9), not by
after-the-fact redaction.

## 3. Architecture

Three parts; ARB Messages' MCP door **and its Postgres `arb_agent_keys` table are untouched**.

- **`arb_crypto`** (new shared module): extract the NaCl primitives from `arb_messages/keys.py`
  (`_validate_public_key`, `seal`, `unseal`, fingerprinting) and **add `Box`** authenticated wrappers.
  **Compat contract (P1, spec panel):** `arb_messages/keys.py` must remain an import surface that
  re-exports `_validate_public_key`, `register_key`, `live_key`, `seal`, `unseal` with byte-identical
  behavior, exceptions, base64/fingerprint output, SQL, and return types — AND preserve
  **module-attribute identity** so existing `monkeypatch.setattr(keys, "live_key", …)` /
  `from arb_messages import keys` call sites still patch the live callable (door_tools references them
  through the module object, not a rebound local). The plan runs the full `tests/arb_messages` suite
  **before and after** the extract; both green is the gate. ARB Messages keeps using `SealedBox`
  (anonymous fulfiller); ARB Secrets uses `Box`.
- **`arb_secrets`** (new module): the bus-native protocol + transport. **Pure Redis. No Postgres at
  all** — not for the mailbox, not for the key registry.
- **Key store:** ARB Secrets has its **own** pubkey registry on the bus (§5), independent of ARB
  Messages' Postgres table. No shared table, no re-keying of live prod, no cross-coupling.

## 4. Crypto

- **Primitive:** NaCl `Box` (X25519 + authenticated encryption). Recipient looks up the sender's
  pubkey from the pinned local store (§5) and `Box`-verifies — a forged envelope `from` fails
  cryptographically.
- **Per-peer keypair:** long-lived X25519 keypair, private half at `~/.arb-secrets/privkey.b64`
  (mode 600, matching the `~/.arb-*/…privkey.b64` convention); pubkey self-published to the bus (§5).
- **`SealedBox`** retained in `arb_crypto` solely for ARB Messages' anonymous-fulfiller delivery.

## 5. Key discovery & trust bootstrap — pure bus-native TOFU

SSH `known_hosts` model, **no Postgres** (spec-panel P0 resolution):

- **Discovery:** each peer self-publishes its pubkey to a bus key
  `agent_scratch:secrets:pubkey:<agent_id>` (distinct namespace from `agent_scratch:agent:<id>:*`).
  This is the *only* place a pubkey lives. There is no Postgres arbiter and no second source of truth
  to disagree with.
- **Trust root = the local pin, not the bus key.** On **first** observation of a peer's pubkey, pin
  its fingerprint locally in `~/.arb-secrets/known_peers.b64`. On every subsequent transfer, verify the
  bus pubkey hashes to the pin. Because the bus key is writable by any participant, the **pin is what
  makes an overwrite harmless** — a swapped bus key won't match the pin and **surfaces to the human;
  it never auto-updates** (the auto-update window *is* the MITM window).
- **First-use (TOFU) window:** inherent to TOFU and **accepted** under mistakes-not-malice — first
  contact is operator-vouched (the operator confirms the fingerprint out-of-band when standing up a new
  peer, same moment they'd hand it bus credentials). Documented as the one residual trust assumption.
- **Identity:** the stable bus `agent_id` (`claude-<project>-<workspace>`) is the keying identity.
- **No PKI, no operator-signed manifest** — heavier than this threat model justifies.

## 6. Protocol

All envelopes ride `kind=notify` with a `secret_*` event. **No sealed body ever travels inline in an
envelope.** Every secret body — `secret_drop`, `secret_request`, AND `secret_reply` — is written to a
TTL'd Redis key and the envelope carries only a **pointer** plus non-secret routing metadata
(from/to/id/expiry). This is the structural guarantee behind §9: the inbox list, the split-watcher
files, the Monitor output, and any tee only ever see pointers and routing metadata, never ciphertext
and never the request's `what`.

**Load-bearing invariant:** a holder responding to a request **always seals to the requester's pinned
pubkey (§5) — never a pubkey supplied inside the request.** A spoofed request is therefore harmless
(the cred is sealed to the real party, unreadable by an impostor).

**A — push (`secret_drop`):** sender writes `Box`-sealed body to
`agent_scratch:secrets:blob:<recipient>:<msg_id>` (`EXPIRE`), then `LPUSH` a pointer envelope
`{event:"secret_drop", from, to, msg_id, expires_at, blob_key}`. Recipient claims via `GETDEL`,
`Box`-verifies sender == pinned `from`, checks `to==self` + `expires_at` (§ clock), replay-guards
`msg_id` (§ replay).

**D — request (`secret_request` → `secret_reply`):**
1. Requester writes a `Box`-sealed body `{req_id (random 256-bit), what, requester_agent_id, expires_at}`
   sealed to the **holder's** pinned key, to a TTL'd blob key; `LPUSH` pointer
   `{event:"secret_request", from, to, req_id, expires_at, blob_key}`. `what` is sealed → confidential.
2. Holder `GETDEL`s, `Box`-verifies the request authenticates as the claimed requester, applies its
   **incoming-request allowlist** + human judgment on `what`.
3. Holder writes a `Box`-sealed reply body `{reply_msg_id (random), in_reply_to: req_id, echo_what,
   secret, expires_at}` sealed to the **requester's pinned key** (invariant), to a TTL'd blob key;
   `LPUSH` pointer `{event:"secret_reply", from, to, reply_msg_id, in_reply_to, expires_at, blob_key}`.
4. Requester accepts iff it (a) `GETDEL`s + decrypts, (b) `Box`-verifies as from the exact holder it
   asked, (c) `in_reply_to` matches an **outstanding** `req_id` it issued, (d) **`echo_what` matches the
   `what` it requested for that `req_id`** (the wrong-secret-under-right-req_id fix — binds the answer
   to the question, not just the correlation id), (e) is unexpired, (f) `reply_msg_id` unseen.

**Outstanding-request store (requester side):** `agent_scratch:secrets:outstanding:<self>:<req_id>` →
`{holder, what, expires_at}`, TTL = request expiry; consulted at step (c)/(d), deleted on accept.

**Replay guard & state machine (both directions):** the claim sequence is ordered —
`GETDEL` pointer → decrypt + `Box`-auth + expiry check → `SETNX
agent_secrets:seen:<self>:<id> EX <T>`; only on `SETNX` success is the payload processed. Marking
seen *after* successful claim+auth (not before) avoids a crash-before-process turning into a false
replay rejection; `GETDEL`'s atomic consume means two notifications for one blob key can't double-
process. **Seen-set TTL `T` = the maximum allowed `expires_at` window (e.g. 24h), not a single
transfer's expiry** — a nonce must stay remembered at least as long as a replay could still be
in-flight.

**Clock domain (cross-host):** `expires_at` is an absolute UNIX timestamp set by the sender; the
recipient compares against its own clock. Hosts are NTP-synced (operator infra); the TTL is generous
(~1h) relative to any skew. Stated so the implementer doesn't invent a relative-TTL scheme.

**Requester authorization:** direction-D is not open to all — a holder applies its own allowlist to
*incoming* requests (symmetric to `AGENT_TRUSTED_SENDERS`) and, being human-attended, exercises
judgment before releasing.

## 7. Delivery / mailbox + status

- Sealed bodies rest in TTL'd keys `agent_scratch:secrets:blob:<recipient>:<id>` (`EXPIRE` ~1h default,
  operator-overridable); the inbox gets only the pointer. Claim = **`GETDEL`** (consume-once, bounded
  rest). Sender generates the id (cross-host collision-free).
- **Status key (in scope):** the sender/holder writes
  `agent_scratch:secrets:status:<sender>:<id>` → `sent`, and the recipient updates it to
  `claimed`/`rejected` on `GETDEL`+verify; it expires with the blob. This makes offline/expiry
  **sender-visible** (poll the status key) without a full receipt protocol, resolving the §1
  contradiction the panel caught. Status values are non-secret metadata only.
- **Offline / expiry:** past the TTL the transfer fails visibly (status never leaves `sent` → sender
  re-sends; direction-D requester re-asks). Check `EXISTS :status` / `LLEN :inbox` (peers-doc §8)
  before diagnosing silence.
- **Named tradeoff:** `GETDEL` consume-once means a recipient crash *after* claim loses the secret
  (re-request) — unlike ARB Messages' `delivered_at` idempotent redelivery. Acceptable: transfer is
  just-in-time; re-request is the recovery path (the status key makes the failure observable).
- **Rejected alternatives:** fire-and-forget `LPUSH` of ciphertext (no per-element TTL → unbounded
  rest, and puts ciphertext in retained stores); Postgres mailbox (claim/lease/fencing over-built,
  inverts bus-native intent).

## 8. Lifecycle

- Keypair: `~/.arb-secrets/privkey.b64` mode-600; pubkey published to the bus key (§5).
- **Rotation (order fixed, spec-panel P1):** generate new keypair → publish new pubkey to the bus key
  → peers see fingerprint mismatch and the **human acks** the new pin (§5) → **retain the old privkey
  briefly to decrypt in-flight blobs sealed to it, then delete.** (ARB Secrets owns its own store, so
  there is no one-live-key DB index to fight; "retain-old-to-drain, publish-new-first" is the correct
  sequence and is now internally consistent.)
- Revocation: overwrite/delete the bus pubkey key + notify peers to drop the pin; peers require a fresh
  operator-vouched first-use to re-establish.

## 9. Observability: structural no-secret-in-retained-store guarantee

The spec panel's central correction: the earlier "redact the observability tee" was aimed at
`visibility_tee`/`claude_tail`, which only carry **engine-dispatch** traffic — peer↔peer `notify`
envelopes never flow through them, so redacting there protects nothing. The real retained stores for
this traffic are the recipient's **split-watcher `/tmp/agent-bridge-inbox/<id>.json`**, the **Monitor
output file**, and the **inbox list**.

**The design makes those stores safe structurally, not by redaction:** because §6 routes every sealed
body (drop/request/reply) through the pointer→TTL'd-blob→`GETDEL` indirection, and seals the request's
`what` inside the body, the *only* content that ever reaches the inbox / split-watcher / Monitor / any
tee is: `event, from, to, msg_id/req_id, expires_at, blob_key`. That is all non-secret routing
metadata. The ciphertext lives only in the TTL'd blob key and is destroyed on claim by `GETDEL`.

**Verification matrix (required real-component tests, one per store):** for `inbox` list, split-watcher
JSON, and Monitor output, assert that after a full push and a full request/reply, **no `secret`,
`what`, `echo_what`, or ciphertext byte appears** — only the pointer fields. Plus an assertion that a
`secret_*` envelope reaching `visibility_tee`/`eval`/`audit` (it shouldn't, wrong plane) would carry no
body. This replaces the vague "tee redaction" task with concrete, testable guarantees and removes the
forward-secrecy retro-decrypt surface (nothing sensitive is retained to decrypt later).

- **Audit:** metadata-only, reusing the `audit_sink` shape — `{from, to, event, id, decision,
  fingerprints, ts}`; never body/ciphertext/`what`.
- **Seal-before-send invariant:** seal in-process *before* the value touches any bus send path.
- **Decrypted-state boundary (documented, not enforced):** protects the wire + at-rest blob, not the
  decrypted-in-heap or written-to-env value. Recipient writes decrypted material only to an explicit
  mode-600 destination and echoes only a fingerprint/label to the transcript.

## 10. Testing strategy

- **`arb_crypto` extract:** run the FULL existing `tests/arb_messages` suite green before and after
  (the gate); `Box` round-trip; tamper→auth-fail; wrong-recipient→fail; fingerprint stability;
  module-attribute-identity test (monkeypatching `arb_messages.keys.live_key` still intercepts the
  door's call).
- **Protocol:** push happy-path; request/reply happy-path; **seal-to-pinned-key invariant** (bogus
  reply-to key in a request is ignored); spoofed request → harmless; **wrong-secret-under-req_id**
  (two concurrent requests, `echo_what` mismatch is rejected); expired payload rejected; replayed
  `msg_id`/`reply_msg_id` rejected; seen-set TTL outlives max expiry; unauthorized requester denied;
  fingerprint mismatch surfaces (no auto-update).
- **Delivery/status:** `GETDEL` consume-once; TTL expiry → status stays `sent` → sender-visible;
  sender-generated-id collision-free.
- **Retained-store guarantee (§9):** real-component test per store (inbox, split-watcher JSON, Monitor)
  asserting no secret content, only pointers.

## 11. Panel provenance

**Design panel** (codex/agy/pi-GLM/cold-Opus, independent): unanimous on `Box`, TTL'd-mailbox+`GETDEL`,
`arb_crypto` extract + new `arb_secrets` + pure-Redis + door-untouched, metadata-only audit. Operator
decisions: build asymmetric now; full formal request protocol (direction D). cold-Opus's `register_key`
"latent bug" verified **refuted** (schema already enforces one-live-key).

**Spec panel** (same seats, independent): 3× FIX_BEFORE_PLAN + 1× PLAN_READY_WITH_NITS. Revision 2
addresses every P0/P1: registry incoherence → **pure bus-native TOFU, ARB Messages table untouched**
(operator-surfaced architecture change); tee redaction wrong-target → **structural no-secret-in-store
guarantee via universal pointer indirection** (cold-Opus P0); reply-binds-question (wrong-secret fix);
rotation order; status-key/failure-visibility contradiction; replay state machine + seen-TTL; clock
domain; `arb_crypto` module-attribute compat contract (see the panel's reports for the full record).

## 12. Open items for the plan

- Exact `arb_crypto` extract boundary + import shim satisfying the §3 compat contract (module-attribute
  identity is the sharp edge).
- The `arb_secrets` module layout: crypto-facing (uses `arb_crypto`) vs Redis-transport vs
  protocol-state; keep units small and independently testable.
- Whether the §9 retained-store tests need a real split-watcher/Monitor harness or can drive the
  producing functions directly (prefer direct-drive per `test-behind-framework-drive-directly`).
