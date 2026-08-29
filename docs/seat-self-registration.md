# One-time seat self-registration

This flow lets an operator authorize a new Buzz-backed ARB seat without sending
the seat private key to arb-buzz. A token is name-bound, optionally host-bound,
hashed at rest, expires, and is consumed when the registrar accepts the signed
request. Approval is accepted only from the configured full Mark pubkey in the
request's Buzz thread.

## Trust and state transitions

1. `seat-token mint` prints a random token once. SQLite stores only SHA-256 plus
   its name, optional host, expiry, and state.
2. `seat-register` creates or reuses a mode-0600 secp256k1 key. Its request is
   ECDSA-SHA256 signed by the same scalar whose x-only public key becomes the
   Buzz identity.
3. The registrar atomically moves a matching active token to `pending`, reads
   back its own member/admin standing in every requested channel, posts a request
   in the ops channel, and mentions Mark. Missing standing is shown prominently
   on that request so it can be repaired before approval. Invalid tokens and
   signatures receive no bus response; only a structured local audit event is
   written.
4. Only an exact `approve <request-id>` / `deny <request-id>` thread reply or a
   ✅ / ❌ reaction from `ARB_REGISTRAR_MARK_PUBKEY` is authoritative. The first
   valid Mark-authored signal wins; a later contradiction is ignored and noted
   in-thread. The registrar reacts 👀 immediately when it accepts the signal.
   Denial and the 24-hour approval timeout burn the token.
5. Approval adds relay and requested-channel membership, verifies both by
   read-back, and sends a provisional response. When optional owner-key custody
   is enabled, that response carries an owner-signed NIP-OA tag authorizing only
   `kind=0`; `seat-register` supplies it to Buzz as `BUZZ_AUTH_TAG` while the
   seat uses its own key to publish its profile. That first publication creates
   the relay `users` row.
6. The registrar reads that profile back, performs the first-write-only
   `agent_owner_pubkey` bind, and sends the final grant only after both succeed.
   It posts denial, success, and bounded-retry failure outcomes in the same
   approval thread. The private key never crosses the registration bus.

The registration signatures are secp256k1 ECDSA over canonical JSON, not Nostr
Schnorr event signatures. They prove control of the same scalar as the x-only
Buzz identity; Buzz CLI separately emits native signed profile events.

## Install on arb-buzz

Create an unprivileged `arb-registrar` service account with access to the Buzz
admin command (if that command uses Docker, grant only the minimum required
socket/proxy capability). Install the package into `/opt/AgentRedisBridge/.venv`,
copy `systemd/arb-seat-registrar.service`, and create
`/etc/arb-seat-registrar.env` from the mode-0600 example. Set the full 64-hex
Mark pubkey; a prefix is deliberately rejected.

The Buzz CLI command must authenticate as a dedicated ARB Registrar relay
identity that is a member of the ops channel. Point
`ARB_REGISTRAR_BUZZ_ENV_FILE` at its mode-0600 file containing exactly
`BUZZ_RELAY_URL` and `BUZZ_PRIVATE_KEY`; these values are merged only into Buzz
CLI subprocess environments. `ARB_REGISTRAR_DATABASE_URL` must
reach the same community database as the relay. After checking the resolved
values without printing secrets:

Owner-key custody is explicitly default-off. If Mark chooses automated NIP-OA
profile authorization, set `ARB_REGISTRAR_OWNER_KEY_FILE` to a protected file
containing Mark's 64-hex or `nsec` key. The file must have no group/other access,
and startup fails unless its x-only pubkey exactly matches
`ARB_REGISTRAR_MARK_PUBKEY`. The private key remains on arb-buzz; only the public
four-element auth tag crosses the registration bus. Without this setting, the
registration behavior remains unchanged and the success thread states that the
auth-tag ceremony is still manual.

For a system service, place the identity file under `/etc` where the
`arb-registrar` account can read it; `ProtectHome=true` intentionally blocks a
key under an operator home. A one-off operator-run acceptance consumer may pass
its existing protected path with `--buzz-env-file`.

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now arb-seat-registrar.service
sudo systemctl status arb-seat-registrar.service
sudo journalctl -u arb-seat-registrar.service -f
```

Token operations run on arb-buzz against the service's state file:

```sh
sudo -u arb-registrar /opt/AgentRedisBridge/.venv/bin/seat-token \
  mint --name throwaway-host-b --host host-b --ttl 24h
sudo -u arb-registrar /opt/AgentRedisBridge/.venv/bin/seat-token list
sudo -u arb-registrar /opt/AgentRedisBridge/.venv/bin/seat-token revoke TOKEN_ID
```

## Register from a remote host

Pass the token out of band. Do not put it in shell history or process arguments
on a shared machine. Prefer a mode-0600 file delivered by ARB's sealed,
consume-once secret transport. The bus env file must use the WireGuard/TLS
endpoint.

`--token-stdin` is also available for a secret-delivery helper that can decrypt
directly into the process pipe without creating a plaintext file.

```sh
export ARB_SEAT_BUZZ_CLI=/usr/local/bin/buzz
seat-register \
  --name throwaway-host-b \
  --host host-b \
  --token-file "$HOME/.config/arb/throwaway-host-b.token" \
  --key-file "$HOME/.config/arb/throwaway-host-b.key" \
  --env-file "$HOME/.config/arb/bridge.env" \
  --channel 66211869-3a75-4f69-881e-fae6fd29bdcf
```

The command waits through both operator approval and profile verification. It
prints `granted` with the relay URL and memberships only after the read-back.
`ARB_SEAT_BUZZ_CLI` is validated before a token file or stdin is read, so a
missing local publisher fails without consuming or reserving the one-time
credential.

## Live Buzz contracts

The registrar deliberately normalizes the deployed CLI and database shapes:

- `buzz-admin add-member` reports successful membership as plain text; commands
  used for ordinary Buzz reads and writes remain JSON-producing.
- `buzz users get --pubkey` returns a JSON list whose live display-name field is
  `display_name`. The reader also accepts the legacy wrapped object and `name`
  field, but always requires the exact requested pubkey and expected name.
- Channel membership reads may be a bare list or a `members` / `items` wrapper.
  Adds are followed by a membership read-back rather than trusted by exit status.
  Before posting the approval request, the same live shape verifies that the
  registrar itself is an `admin` or `owner` in each requested channel. Missing,
  lower-role, and unreadable standing are written onto the approval request.
- Provisioning failures retain the affected channel id in every retry message,
  terminal thread result, client denial, persisted error, and audit event.
- PostgreSQL stores `users.pubkey` and `users.agent_owner_pubkey` as `bytea`.
  Owner binding therefore uses decoded bytes for both the seat lookup and Mark
  value; idempotency compares returned bytes, not hex text.
- Approval messages, replies, reactions, and channel-member response shapes were
  exercised against the deployed relay during acceptance. ✅ / ❌ reactions and
  exact typed replies remain equivalent decision inputs; 👀 acknowledges the
  first accepted signal before provisioning begins.
- An automated NIP-OA tag is canonical compact JSON:
  `["auth","<owner-xonly>","kind=0","<bip340-signature>"]`. The signature is
  over `SHA256("nostr:agent-auth:<seat-xonly>:kind=0")`; the implementation's
  generated fixture was accepted by the deployed Buzz CLI's strict parser and
  verifier before its deliberately unreachable relay produced the expected
  network error.

## Live acceptance and cleanup

Use two fresh tokens and one throwaway identity per path:

1. Denial: mint, register, observe the threaded request, have Mark reply
   `deny <id>`, and prove the client receives denial and the token is `denied`.
2. Approval: mint, register, reply `approve <id>` or react ✅, and prove relay
   membership, profile read-back, then first-write owner binding, requested
   channel membership, and the client's final correlated grant.
3. Cleanup: use `buzz-admin remove-member --pubkey <pubkey>`, remove the seat
   from every requested channel, delete the throwaway profile/user if the Buzz
   deployment exposes that operation, remove the local throwaway key, and run
   `seat-token revoke <token-id>` when it is not already terminal. Query global
   and channel membership afterward; a successful command without the read-back
   is not sufficient evidence. If the deployed Buzz surface has no safe user
   deletion operation, record the orphan profile row and its owner explicitly;
   zero global and channel memberships are the effective access revocation.

Retain request IDs and audit timestamps, never tokens or private keys, in the
acceptance record.

## Recovery

SQLite updates use `BEGIN IMMEDIATE`, so two consumers cannot spend one token.
The systemd unit restarts on all exits. Requests in `pending` or `provisioned`
survive restart; approval polling and profile-ready handling are idempotent at
the store state boundary. Provisioning and profile verification retry at most
five times with backoff; exhaustion closes the request, burns the token,
notifies the client, and reports failure in-thread. If external provisioning
partly succeeds, retries accept the admin command's plain-text success. Owner
binding runs only after profile read-back because the seat's first profile
publication creates its `users` row; that bind is idempotent only when the
existing owner is Mark, and a different owner still fails closed.

A client crash after the provisional response leaves the request in
`provisioned`. Prefer fixing the local prerequisite and completing the same
request: republish the profile idempotently, then send a seat-key-signed
`seat_registration_profile_ready` carrying the persisted request id,
`client_nonce`, pubkey, and expected profile name. Consume a grant only from the
original reply inbox and only when request id, pubkey, nonce, registrar sender,
and `profile_verified=true` all correlate. If that bounded request has already
gone terminal, do not reopen it or replay its token; mint a fresh token and run
the complete flow again.
