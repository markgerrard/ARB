# ARB Comms — fork plan (buzz as base)

Status: **draft for review**, 2026-08-02. Supersedes nothing; complements the
control-plane handoff (ARB Memory `art-4a69641bf02ec070`).

## Decision

Fork `block/buzz` as the base for **ARB Comms** — an operator control centre for
driving ARB warm orchestrators over ACP, plus a colleague-facing surface later.
Upstream is retained as a remote for occasional rebase, not as a dependency.

**Why fork rather than consume.** The consumer posture (pin + overlay, zero
tracked modifications) was correct while buzz was a black box we deployed. It
stops being correct the moment we need behaviour buzz does not have: agent
replies delivered to channels, real mid-turn steering, a spend ceiling, and a
control-centre view across N channels. Every one of those is a change to code we
already run on our own hosts.

**Why buzz rather than build.** The relay is the best-engineered part and the
part we least want to write: host-derived multi-tenancy that fails closed, a
database-resident lint that fails the build if a table lacks `community_id`,
per-channel ACL, NIP-42/NIP-98 auth, Blossom media, Redis fan-out. The clients
are working chat apps on three platforms. We inherit all of it for the cost of a
rebrand.

**Precedent.** We forked codex and rebased 0.144 → 0.146 cleanly with AI
assistance. The merge-burden objection is answered by our own practice.

## Licence position (Apache-2.0)

Verified against the upstream tree at pin `ac4fa13b8`:

| Fact | Consequence |
|---|---|
| `LICENSE` is Apache-2.0 | Retain it in the fork (§4(a)) |
| **No `NOTICE` file** | §4(d) attribution-file obligation does not apply |
| **No per-file copyright headers** | §4(c) has nothing to retain beyond the licence |
| §4(b) | Modified files must carry prominent notice that we changed them |
| §6 — no trademark grant | We **must not** ship under the Buzz name/marks. Rebrand is required, not optional |
| No copyleft | No obligation to publish our changes |

Not legal advice. If ARB Comms is ever distributed outside example-org, have the
licence position reviewed.

## Repo and remote strategy

- Fork to `example-org/arb-comms`.
- `origin` → our fork. `upstream` → `https://github.com/block/buzz` (public,
  fetchable anonymously — verified).
- Keep the existing bare mirror on the offline build host as the offline copy.
- Rebase from upstream **on our cadence**, tag-to-tag, using the codex workflow.
  There is no obligation to track; upstream is insurance, not a schedule.

`deploy/buzz/UPSTREAM_REF` and `deploy/buzz/buzz-deploy` exist to police a
consumer relationship we are ending. They do not disappear — they change shape:
the pin becomes "the upstream commit we last rebased from", and `buzz-deploy`'s
refusal-on-drift becomes a check against *our* release tag.

## Principle: surgical divergence, defer deletion

**Do not strip unused subsystems yet.** Huddle audio, mesh-compute,
git-on-object-store, forum, personas, teams, workflows and custom emoji are all
dead weight for a control centre — and deleting them fights every future rebase
across thousands of files. Unused code behind a VPN costs almost nothing;
deleted code costs us at every merge.

Delete later, once we are certain we will never pull again. That decision is
free to defer and much easier to make in six months.

## Patch set — what changes immediately

### `buzz-acp` (our binary, on our seats — highest value, zero distribution cost)

1. **#2698 — deliver agent replies to the channel.** `acp.rs:1715-1720` logs
   `agent_message_chunk` text and drops it. Accumulate and publish as kind:9 at
   turn end; buzz-acp already publishes kind:9 in production (dead-letter
   notices). This deletes the workaround line from every seat prompt forever.
2. **A respond-to mode that excludes siblings.** `lib.rs:249-257` resolves both
   `owner-only` and `allowlist` through `is_owner_or_sibling`, so every agent
   sharing our owner key can drive every other agent's orchestrator. Add a
   strict mode. See "Open decisions".
3. **A spend ceiling.** There is none — only idle and turn-duration timeouts.
   Kind 44200 already carries `cost_usd` per turn; enforce a per-channel daily
   cap against it.
4. **`ALLOWED_MIMES` — accept `application/pdf`.** `buzz-cli/src/client.rs:64-70`
   rejects PDFs client-side; the relay accepts them fine via `PUT /upload`.
   Genuinely upstreamable as a plain bug fix.

### `src/arb_warm_orch/` (ARB side — small, and blocking the first proof)

5. **Honour `params.systemPrompt` in `session/new`.** We answer
   `protocolVersion: 2`, so buzz uses the modern delivery path and writes the
   system prompt into `session/new` params — which `acp_server.py:208-211`
   ignores. Everything vanishes, including the #2698 mitigation. Stopgap:
   declare `PROTOCOL_VERSION = 1` and buzz injects the sections per-turn
   instead. (Moot once patch 1 lands, but the prompt still matters.)
6. **Implement `_session/steering`.** Without it buzz falls back to
   cancel-and-merge: a mid-turn message kills the in-flight turn and replays the
   *original* request, discarding partial work. For a control centre whose point
   is steering, this is the difference between working and infuriating.
7. **Launch config per seat.** `BUZZ_ACP_IDLE_TIMEOUT=6000` (default 900 kills
   any dispatch over 15 min — the runner emits nothing while blocked in
   `dispatch_seat`), `BUZZ_ACP_MAX_TURN_DURATION=9000`,
   `BUZZ_ACP_MULTIPLE_EVENT_HANDLING=queue` until patch 6 lands.
8. **A capability ceiling on the orchestrator.** `runner.py:131` grants
   `Bash, Read, Write, Edit, Glob, Grep, dispatch_seat` pre-approved, and the
   only PreToolUse gate knows merge/close. Our own `agent_sdk.py:415-454`
   refuses to boot a *worker* without a provably fail-closed gate and a deny
   sentinel. Extend that posture to the orchestrator **before** the write path
   is network-reachable. This is ARB work, cheap, and worth doing regardless of
   buzz.

### Relay — configuration only, no fork needed yet

9. `BUZZ_REQUIRE_MEDIA_GET_AUTH=true` — currently off, so media is served to
   anyone who knows the sha256. Verified live: an uploaded PNG is retrievable
   unauthenticated from the public internet. Fix before any transcript-derived
   document exists.
10. `BUZZ_CORS_ORIGINS` — unset falls back to `CorsLayer::permissive()`.
11. **Two-entry `ports:`** — bind a private VPC address on `:3000` alongside
    `127.0.0.1:3000`, plus **split-horizon DNS** so `buzz.example.com`
    resolves privately. One change solves three problems: VPN perimeter, exact
    `Host` for tenant routing, and media URLs resolving down the private path.

## Rebrand (do early — it threads through build config)

Required by §6 if we distribute at all.

- `desktop/src-tauri/tauri.conf.json`: `productName: "Buzz"` and
  `identifier: "xyz.block.buzz.app"`.
- Flutter: `BUNDLE_IDENTIFIER` is already a build variable
  (`mobile/ios/Flutter/*.xcconfig`, `DEVELOPMENT_TEAM = ""`) — the project is
  built to be rebranded.
- App names, icons, splash, deep-link scheme (`buzz://`), and the relay's
  NIP-11 `name`/`description`.

## Build and distribution

| Component | Path |
|---|---|
| Relay | Keep `ghcr.io/block/buzz` until we patch the relay; then our own image |
| Desktop | Tauri build on the offline build host; Developer ID signing + notarization |
| Mobile (iOS) | **No upstream binary exists** — we build either way. Apple Developer account, own bundle ID, TestFlight internal (no App Review for internal testers) |
| Mobile (Android) | `flutter build apk`, sideload |
| Push | Own APNs key + our own `buzz-push-gateway` deployment. App Attest is push-only — it does **not** gate client→relay connection. Defer |
| Web client | Later: port the desktop relay engine (1,816 lines of portable TS, three Tauri seams) behind CF Access |

## Sequencing

**Phase 0 — prove the loop (hours, do this first).**
A posted message becoming a turn **has never once worked** (the internal orchestration log
entry 32: handshake succeeded, `session/prompt` never arrived). Patches 5 and 7,
one scratch channel, one seat, `#project-b-dev`. Proofs: round trip; warmth
across respawn; the merge/close gate still refuses *through the buzz path*; a
20-minute dispatch survives. If this fails, everything else is premature.

**Phase 1 — fork and patch.** Fork, rebrand, land patches 1–4 and 6, apply relay
config 9–11. Stand up three Workflow 1 seats: `#project-b-dev`,
`#project-a-dev`, `#project-f`. One buzz-acp per seat, one keypair, one channel,
`--agents` per concurrent domain.

**Phase 2 — close the ceiling (patch 8), then open the write path** beyond
owner-only.

**Phase 3 — production domains.** `#project-a` on host-d only. **Do not** put
buzz-acp on the legacy service boxes; host-d keeps fanning out over the
existing Redis peer bus. Buzz sits *above* Workflow 2, it does not replace it.
SSH + tmux stays declared primary for incident work.

**Phase 4 — staff.** Web client behind CF Access (Access protects nothing today
because there is no browser client). Read-only DB role, typed MCP query tools,
no free-text SQL, no `dispatch_seat` on staff seats. Then the document pipeline.

## Open decisions (the first two are hard to reverse)

1. **Community-per-customer vs channel-per-customer.** `channels.community_id`
   is immutable by database trigger; there is no supported re-tenanting path.
2. **Host naming scheme.** `communities.host` is the tenant selector; media URLs
   and git remotes derive from it. Changing it later re-points every client.
3. **Dispatch wire** — Nostr kinds `43001-43006` (a reserved job protocol with
   nothing implemented in it) vs the existing ARB bus. Determines where the
   audit trail lives.
4. **Do seats share an owner key?** If yes, the fleet is one trust domain and
   patch 8 carries the whole load. If no, patch 2 becomes the control.
5. **Staff key custody** — behind CF Access the Nostr key is an identity token,
   not the access control, which makes browser-generated keys defensible.

## Risks accepted

- **No upstream security patches.** ~120k lines of Rust we did not write.
  Rust removes the memory-safety class; logic bugs remain. Mitigated by the VPN
  perimeter and a small user set — this is the main thing the perimeter buys.
- **Three toolchains** (Rust, Tauri/React, Flutter), three release paths.
- **TestFlight builds expire ~90 days**; internal distribution needs a rebuild
  cadence.
- **Prompt injection is structural, not incidental.** `queue.rs:1076-1109`
  interpolates event content raw alongside the `From:` attribution, and
  `pool.rs:1897-1902` hoists sender content *ahead* of the buzz context. Customer
  transcripts are attacker-influenceable. The tool layer must be the boundary;
  the system prompt cannot be.
