# Fleet Restart Runbook

Use when restarting live bridge seats or rolling out bridge code across a shared bus.

## First: Confirm The Rollout Is Actually Owed

For launchd Python daemons the running code is frozen at process start, so "did seat X
already pick up commit Y" is decidable from three static facts — no dispatch, ping, or
probe:

1. `ps -p <pid> -o lstart` — when the daemon loaded its code.
2. `git -C <fleet-clone> reflog --date=iso` — when the clone reached the code-bearing SHA.
3. The plist's `PYTHONPATH` / `ProgramArguments` — proves *which* tree the daemon loads
   (guards against per-project clones and venv shadowing).

Start time later than the SHA's arrival on the pinned tree ⇒ already deployed; confirm the
mechanism is in that SHA with `git grep <marker> <sha> -- src/` and skip the restart
entirely. Corollary: any event that restarts many daemons (a launchd promotion, reboot
recovery) silently resolves every pending code-rollout item for those daemons — sweep your
pending-rollout list whenever one happens. (Provenance: the AGY-2 rollout was carried as
owed for two days, 2026-07-08→10, when the Jul-8 launchd promotion had already deployed it
the same afternoon the "not yet on seats" note was written. ARB Memory
`art-8a92f22ff60dbf60`.)

## Before Restarting A Seat

Check every running task status on the bus before killing any live daemon:

```bash
redis-cli --scan --pattern 'agent_scratch:task:*:status' |
  while read -r key; do
    redis-cli HGETALL "$key"
  done
```

Look for `state=running` entries and inspect their `updated_at` values against the
seat's turn timeout. Stale running entries are common; never sample one task and
generalize to the whole fleet. The bus hosts multiple concurrent orchestrator
sessions, so a fresh running task that is not yours means coordinate before restart.

## Restart Launchd-Managed Seats

Launchd-managed seats use labels such as `com.example.*-bridge.*`. Restart them with
`launchctl kickstart`, not kill-plus-manual-respawn:

```bash
launchctl kickstart -k gui/<uid>/<label>
```

Killing a launchd-managed process and then manually spawning a replacement can race
launchd's own resurrection. The result is two consumers on one inbox, which makes
delivery nondeterministic.

## Reliable Inbox Recovery

Since `8fa684d`, the reliable inbox path moves in-flight envelopes into
`agent:<id>:processing`. A mid-turn kill parks the envelope there; after restart,
the daemon recovers and re-runs it.

That recovery is intentional, but side-effectful tasks may not be idempotent. Before
restarting a seat with a fresh running task, check whether re-running the current
envelope could repeat an external side effect. See `SPEC.md` for the protocol and
idempotency expectations.

## Post-Restart Verification

Verify all of these before declaring the fleet healthy:

1. The daemon log contains `[bridge] <agent_id> online` for every restarted seat.
2. The daemon is blocked in `blmove`, not `blpop`, confirming the reliable inbox code
   path is active.
3. A one-message end-to-end ping dispatch succeeds through the restarted seat.

Example ping:

```bash
scripts/agent-bridge-ping dev
scripts/agent-dispatch --target-id <agent-id> --timeout 600 \
  --run-id "fleet-restart-ping-$(date -u +%Y%m%dT%H%M%SZ)" \
  "Do not edit files. Reply with bridge restart ping ok."
```

## Escalate If

- Any running task has a fresh `updated_at` and you do not own the orchestrator session.
- Two processes are consuming the same `agent_id` inbox.
- A recovered task would repeat an unsafe side effect.
- The restarted daemon still blocks in `blpop`, which means old code is running.

## Provenance

Captured from the 2026-06-10 rollout lessons: stale running task statuses were present
on a shared bus, launchd resurrection created a double-consumer risk, and reliable
inbox recovery changed the mid-turn kill semantics from message loss to re-run.
