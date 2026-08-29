# Bus-tap logging — passive, token-free chatter archive

Log every envelope on the agent bus (lead↔nodes AND node↔node) to dated JSONL files,
with zero LLM involvement and zero risk to delivery. 14-day retention by default.

## Why not just "watch" the bus

Inboxes are Redis **lists** consumed by `BLPOP` — there is no passive read. An observer
either races real recipients for the pop (it would *steal* messages) or sees nothing.
`MONITOR` exists but firehoses every command on every DB of the cluster through the
observer — unacceptable on a shared production Valkey.

## Design: tap stream, dual-written at send time

1. **Every sender** adds one best-effort command after the inbox `RPUSH`:

   ```bash
   XADD ${PREFIX}tap MAXLEN '~' 10000 '*' envelope "$ENVELOPE"   # || true — never block delivery
   ```

   Streams are fan-out readable: the tap consumes nothing, the delivery path is
   untouched, and node↔node traffic is captured because the tap lives in the *send*
   convention, not in any recipient.

2. **One logger daemon** (`scripts/bus-tap-logger.py`, systemd-managed, runs on the lead
   host or anywhere with bus creds): `XREAD BLOCK` from the last persisted stream id →
   one JSONL line per envelope (`{logged_at, stream_id, envelope:{...}}`) into
   `agent-bus-YYYY-MM-DD.log` → date-based self-rotation + deletion past
   `TAP_RETENTION_DAYS` (default 14).

Properties that fall out:

- **Restart-safe / gap-free**: last-id persisted; the stream's `MAXLEN ~10000` buffer
  covers logger downtime — it catches up on restart rather than losing the interval.
- **Bounded**: Redis holds ≤ ~10k envelopes; disk holds 14 days (file_drops carry whole
  documents, so expect single-digit MB on busy days).
- **Forensics-grade**: `grep <event_id> agent-bus-*.log` replaces cross-node archaeology
  for "who sent what when" questions.

## Install (logger host)

```bash
# /etc/systemd/system/agent-bus-logger.service
[Unit]
Description=Agent bus tap logger (passive JSONL archive)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<OPERATOR_USER>
Environment=ENV_FILE=<PATH_TO_ENV_WITH_REDIS_CREDS>
Environment=TAP_LOG_DIR=/home/<OPERATOR_USER>/agent-bus-logs
ExecStart=/usr/bin/python3 <BRIDGE_CLONE>/scripts/bus-tap-logger.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Requires `python3-redis` (redis-py). The logger reads `REDIS_*` or `AGENT_REDIS_*`
names from `ENV_FILE`; `AGENT_REDIS_TLS=` (empty) for non-TLS local buses.

## Sender rollout

- v0 bash senders (`send.sh` / `send_file.sh`): the one-XADD block after `RPUSH`.
- Raw `redis-cli LPUSH` senders (peer-coordination Step 4 style): add the same `XADD`
  alongside; `|| true` it.
- Bridge daemon (`agent_redis_bridge`): candidate follow-up — mirror outgoing envelopes
  to the tap in `redis_io` so engine-dispatch traffic is captured too.

**Convention status:** senders SHOULD tap; the logger tolerates non-tapping senders
(their messages simply don't appear — coverage degrades, nothing breaks). Audit coverage
by comparing inbox traffic you *know* happened against the log.

## What this is NOT

- Not delivery-critical: tap failure must never fail a send (`|| true`).
- Not a privacy boundary: anyone with bus creds reads the tap stream, and anyone in the
  log dir's owner-group reads the files. Same rule as the bus itself — coordination text
  and code, never secrets *through the tap stream*. (Files are created dir `0750` /
  `0640`, owner+group only, never world-readable — but that's defense-in-depth, not a
  licence to tap secrets.)
- Persistence cuts both ways: a secret sent in-band lands **at rest** in the recipient's
  inbox log and, if a sender feeds the tap stream, in the dated archive too. Note
  `a legacy bash peer script (case study not included in this repository)`
  (the minimal project-a sender) only `RPUSH`es the inbox and
  does **not** XADD the tap stream — so secrets relayed via it are *not* tapped, but they
  still persist in the recipient's inbox jsonl. Either way: **rotate or scrub an in-band
  secret after its operational window** rather than leaving a plaintext copy at rest.
- **Prefer rotation over scrub, and never grep for the secret *value*.** Scrubbing is a
  chase across hosts (inbox jsonl, agent session transcripts, journald, `auth.log`) and a
  `grep`/`sudo` whose argv contains the secret leaks a fresh copy into shell history +
  the journal + `auth.log` — the scrub creates copies faster than it removes them. If you
  must scrub, match by **msg-id or file path**, never by the secret string. Rotation
  kills every at-rest copy on every host in one move; it's the structural fix, scrubbing
  is the discipline one. (Learned live relaying the minimax key, 2026-06-08.)
- Not a replacement for `task:<id>:result` persistence on engine dispatches.
