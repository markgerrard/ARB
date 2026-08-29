# Codex as an ARB Workflow 2 peer

This runbook covers an interactive Codex CLI session acting as a peer on the ARB
coordination bus. It is the Codex counterpart to
[`docs/claude-peer-coordination.md`](../claude-peer-coordination.md): no engine
dispatch and no bridge seat. Two interactive sessions exchange `kind=notify`
envelopes through Redis/Valkey.

Status: the first live Codex peer test began 2026-08-02 with
`codex-arbcomms-host-b` talking to `claude-arbcomms-arbbuzz`. Native
background-terminal wake and the first two-way handshake are proven. Maintain
the observations section as the workflow matures.

## The Codex-specific shape

Claude Code uses a persistent Monitor task. Codex has no Monitor tool. The ARB
Codex fork instead wakes the interactive session when a background terminal
process exits.

Use two independent ARB-side processes:

1. Start `scripts/codex-peer-heartbeat` once for the seat session. It refreshes
   `agent_scratch:agent:<agent-id>:status` continuously, including while Codex
   is processing an envelope and no receiver is armed.
2. Start `scripts/codex-inbox-once` as the single process which owns this
   session's inbox.
3. When one envelope arrives, it asserts the `to` field equals this session's
   agent ID, preserves the full envelope, and exits.
4. The terminal exit opens a new Codex turn through native background wake.
5. Collect the terminal output, process the envelope, and immediately arm the
   next receiver.

Both scripts talk to Valkey with `redis-cli`; the Codex fork's terminal-exit
wake is generic and has no ARB or Redis knowledge. Liveness and listening are
different facts: the persistent heartbeat proves the seat is alive, while the
one-envelope process exists only while the inbox is armed. Workflow 2 currently
publishes only liveness; add a separate listening key only if operations prove
it necessary.

There must be exactly one inbox owner per agent ID. Two `BLPOP` consumers race
and one silently steals the other's message.

## Bootstrap checklist

1. Read `skills/using-agent-bridge/SKILL.md` and the task-specific ARB Memory
   bootstrap artefact completely.
2. Set an honest identity, conventionally `codex-<project>-<workspace>`.
3. Keep bus credentials in a mode-600 env file. Never print or commit the
   password.
4. Confirm the Redis database with the peer. Managed Redis commonly defaults to
   DB 0; never guess an established coordination bus's DB.
5. Start the persistent heartbeat and confirm its status TTL is positive.
6. Prove native background wake before relying on it.
7. Arm the inbox receiver, then send `peer_online` as `kind=notify`.
8. Do not begin dependent work until the peer confirms the link.

When the bootstrap names a coordination lead, treat that assignment as the
authority boundary: the lead sequences shared work and resolves peer-level
decisions; Codex owns tactics on its host, reports blockers with evidence, and
does not silently broaden the task or mutate infrastructure.

## Native background-wake self-test

Run a terminal command whose initial tool call returns while it is still alive:

```sh
sleep 8
echo CODEX_BGWAKE_SELFTEST_FIRED
date -u --iso-8601=seconds
```

End the Codex turn after retaining the terminal session ID. Passing evidence is
all three of:

- a new Codex turn starts without operator input when the process exits;
- injected context names the background terminal exit and status;
- collecting the retained terminal returns `CODEX_BGWAKE_SELFTEST_FIRED`.

The repository's current `scripts/arb-bg-wake-selftest` is a Pi/macOS
file-inbox test with host-specific paths. It does not prove the Codex fork's
native terminal-exit wake on Linux.

## Start the heartbeat and receiver

Load the managed bus environment with auto-export so both scripts inherit it:

```sh
set -a
source /path/to/agent-bus-managed.env
set +a
AGENT_ID=codex-<project>-<workspace> scripts/codex-peer-heartbeat
```

Keep that terminal process alive for the whole seat session. In a separate
background terminal, arm the one-envelope receiver:

```sh
AGENT_ID=codex-<project>-<workspace> scripts/codex-inbox-once
```

After each receiver exit, collect and process its single JSON line, then launch
`codex-inbox-once` again. Do not launch a second receiver for the same agent ID.

## Process requirements

The heartbeat and receiver must:

- source the bus env without echoing secrets;
- set `REDISCLI_AUTH` so the password is absent from process arguments;
- keep heartbeat ownership independent of inbox consumption;
- refresh `status` to `alive:<unique-owner-token>` with `EX 60` every 20
  seconds by default;
- block only on `agent_scratch:agent:<this-agent-id>:inbox`;
- use the configured database explicitly;
- validate JSON and require `to == <this-agent-id>`;
- preserve the complete envelope, not a truncated preview;
- delete the status key on heartbeat shutdown only if the key still contains
  that heartbeat process's owner token.

Managed Redis can close an indefinitely idle `BLPOP`. The receiver uses bounded
calls below the provider idle timeout and loops internally. It exits only after
one valid envelope or a terminal error; the independent heartbeat remains alive.

## Sending coordination envelopes

Workflow 2 uses `kind=notify`; `request` and `reply` belong to engine dispatch.
The minimum envelope is:

```json
{
  "id": "<uuid>",
  "from": "codex-<project>-<workspace>",
  "branch": "dev",
  "to": "<peer-agent-id>",
  "kind": "notify",
  "sent_at": "<iso-8601>",
  "payload": {"event": "<event-name>", "data": {}}
}
```

Send with `LPUSH <prefix>agent:<peer-id>:inbox <json>`. If the peer's inbox
length returns to zero within about a second, its consumer accepted the message;
that proves consumption, not agreement or task completion. Read dialects
liberally, but enforce the recipient identity locally.

Validate the serialized envelope before `LPUSH`: require a non-empty value,
parse it back as JSON, and check its required fields. Shell command substitution
can yield an empty string when the serializer fails while a later unguarded
`LPUSH` still succeeds, consuming an empty inbox item that looks like transport
activity but carries no evidence.

## Turn discipline

- Re-arm the receiver immediately after processing each envelope.
- Keep the receiver armed until the human operator explicitly says to stand
  down. A lead instruction to "stop", "report and stop", or "do not start the
  next patch" stops substantive task work; it does not terminate Workflow 2
  presence or inbox monitoring unless the operator also ends the peer session.
- Keep long local work in a background terminal so its exit wakes Codex.
- A terminal exit is only a wake signal; collect retained output before claiming
  success.
- Earlier short terminal commands also generate wake events. Match the expected
  process/session before interpreting a wake.
- Put decisions and durable evidence in git or ARB Memory. The inbox is
  transport, not a system of record.
- Do not poll Redis for progress.
- Report a blocker as: exact exit status, last independently verified state,
  whether substantive work started, unchanged constraints, and the narrow next
  step. Wait for lead direction when that next step changes target, source,
  toolchain, infrastructure, or another material assumption.

## First live observations (2026-08-02)

Observed on Codex CLI `0.146.0-arb` on Ubuntu 24.04:

- A background terminal returned a live session handle, later exited, and opened
  a new Codex turn without operator input.
- Collecting it returned the expected marker and timestamp.
- `codex-arbcomms-host-b` published a 60-second heartbeat and sent `peer_online`
  to `claude-arbcomms-arbbuzz` on managed-bus DB 12.
- The peer consumed the handshake within one second and returned a valid
  `build_greenlight` envelope addressed to the Codex identity.
- The receiver's exit triggered another wake; collecting its output returned the
  complete JSON envelope.
- Unrelated terminal exits can arrive near the expected receiver wake. Retained
  terminal identity is part of the evidence chain.
- The lead accepted a capability envelope containing host, OS, kernel,
  architecture, CPU/vCPU, RAM, disk, glibc, Codex version, and toolchain state,
  plus an explicit acknowledgement of lead authority.
- A missing `/usr/bin/time` produced status 127 after Hermit verified Rust and
  Cargo 1.95.0 but before compilation. Codex reported the failure and proposed
  only Bash's built-in `time`; the lead approved it and explicitly rejected
  installing a package merely to collect the metric.
- The unchanged native retry built stock `buzz-acp` in 65.640 seconds, after
  which Codex independently checked the executable, SHA-256, `ldd`, `--help`,
  commit pin, clean source state, and ARB Files object metadata before reporting
  completion.
- A Python `true`/`True` typo made one serializer return an empty value; because
  the send shell lacked a post-substitution guard, it pushed an empty item. Codex
  immediately sent a valid corrective envelope naming the malformed predecessor.
  Future sends must validate before `LPUSH` as specified above.
- The lead independently fetched and verified the first delivered binary, then
  assigned the next task through a complete addressed envelope. Codex kept the
  receiver re-armed while implementing locally, reported a missing fork-creation
  capability without inventing a workaround, and sent a validated pre-push
  package containing diff lines, design answers, tests, and binary evidence.
- A lead instruction such as "do not push until I have seen the diff" is a real
  phase gate: local branching, edits, formatting, tests, and builds may proceed;
  commit/push waits for the lead's explicit response. Report environmental gaps
  separately so they do not get hidden inside an otherwise green code report.
- When the lead asks for the diff, deliver the actual patch bytes rather than
  only a prose summary. For a non-trivial patch, store it in ARB Files, send the
  exact `agent-files/...` reference, byte count, SHA-256, immutable base commit,
  test evidence, and current commit/push state in a validated envelope.
- Treat review replies that arrive while local work is running as authoritative
  phase input, even if a later local revision already addresses part of them.
  Answer each named question against the current code, refresh the actual diff
  artefact, and give it a new immutable name/hash so the lead never reviews a
  stale byte stream under an old reference.
- The one-envelope receiver may immediately consume a queued review and exit
  during re-arm. Process that envelope first, then arm a fresh receiver before
  sending the follow-up; a successful re-arm is not synonymous with a receiver
  that remains blocked.
- When cancellation and retry interact, report the user-visible semantics, not
  only the transport mechanics. In the first patch review, the lead caught that
  publishing a partial response on default `Steer` cancellation would leave a
  half-reply followed by the retried full reply. The final local design publishes
  a partial only for explicit `ControlSignal::Cancel`; requeueing control signals
  suppress it.
- A reply-delivery path should state its guarantee precisely. Retrying the same
  signed event ID gives deduplicated transport retries, while requeueing the
  whole turn after an ambiguous terminal response is at-least-once and can
  theoretically produce a second reply under a new event ID.
- Repository creation can hide an irreversible visibility decision. A GitHub
  fork of a public upstream remains public; a private downstream requires a new
  private repository with upstream retained as a Git remote. Escalate that
  choice and keep working locally—do not let the remote's eventual GitHub UI
  relationship silently decide confidentiality.

Not yet proven:

- multi-hour idle reconnect behavior;
- rapid back-to-back messages during the re-arm window;
- recovery after Codex CLI restart;
- clean identity handoff to a successor Codex session.

## Related references

- `skills/using-agent-bridge/SKILL.md` — protocol and operational authority
- `docs/claude-peer-coordination.md` — mature Workflow 2 transport lessons
- `docs/orchestrating-claude-peers.md` — lead workflow for three or more peers
- `docs/runbooks/bridge-dispatch-completion-generic.md` — retained terminal
  session and notification concepts
