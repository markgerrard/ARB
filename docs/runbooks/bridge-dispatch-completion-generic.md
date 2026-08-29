# Bridge dispatch completion for Codex and generic orchestrators

This is the harness-neutral completion and observation contract for an
AgentRedisBridge engine dispatch.

## The sender: dispatch is the wait

`scripts/agent-dispatch` pushes the request, then blocks in a `BLPOP` loop on the
sender's inbox. It accepts only a `kind=reply` envelope whose `in_reply_to` equals
the dispatched task ID. The dispatcher exits after that reply: exit 0/1 reflects
the reply and 124 means timeout. Its process exit is the completion notification.

- Run the dispatcher as a real foreground or harness-managed background process.
- Keep its process/session handle and wait on that handle.
- Do not pseudo-detach it inside another shell background expression.
- Capture stdout for the final payload and stderr for the printed `task-id: <uuid>`.

### Keep the chat responsive: yield one persistent completion watcher

Do not repeatedly issue short foreground waits: the UI will remain in a
"waiting for tool call" state even though the worker is correctly backgrounded.
Instead, start one yielded watcher that renews the bounded transport wait
internally until the dispatcher process exits. A single bounded
`write_stdin(..., yield_time_ms=300000)` is not persistent: it can return a live
`session_id` after five minutes and leave no watcher armed. Conceptually:

```js
let sessionId = dispatcherSession;
yield_control();
while (sessionId) {
  const result = await tools.write_stdin({
    session_id: sessionId,
    chars: "",
    yield_time_ms: 300000,
  });
  if (result.exit_code !== undefined || !result.session_id) {
    notify(JSON.stringify({
      event: "bridge-dispatch-complete",
      result,
    }));
    break;
  }
  sessionId = result.session_id;
}
```

This is the Codex equivalent of Claude Code's
`Bash(run_in_background=true)`: the dispatcher stays attached to its own BLPOP,
the harness delivers completion when the process exits, and the conversation
remains responsive meanwhile. Do not create a timer/poll loop around it.

The terminal branch should call `notify()`, not `text()`. `text()` stores output on
the background cell, but does not start a new model turn; it remains invisible
until a later caller collects that cell. `notify()` immediately injects a
`custom_tool_call_output`. On 2026-07-14 this automatically woke the orchestrator in
verified by one initial five-second probe plus six sequential five-second runs:
all seven notifications arrived without a manual cell check.

The renewal loop and short-delay wake have separate evidence. A production Luna dispatch
proved the loop stayed armed across a bounded-wait renewal and captured terminal
output. The seven probes proved `notify()` can supply the missing automatic wake.
However, a later six-minute verification watcher captured and queued its terminal
notification without starting a new assistant turn; the output surfaced only
after a user message and explicit cell collection. Long-idle automatic wake is
therefore not reliable on this runtime.

Use a hybrid for autonomous progression: retain the persistent result-capturing
watcher, and schedule one coarse watchdog check just beyond each bounded-wait
window (about 310 seconds for a 300-second wait). If the dispatcher is still
running, re-arm one watchdog for the next window. This is bounded fallback polling
at most once per five minutes, not continuous task/event polling. The watcher
preserves the result; the watchdog guarantees a model turn when long-idle notify
delivery does not.

If the harness cannot yield a running tool call, leave the dispatcher session
alive and resume it only when completion is requested; observe third-party
progress solely through the durable snapshots below.

## Pi-harness orchestrators

A pi-harness orchestrator (e.g. Grok or GLM driving the pipeline under pi) follows the same
contract with one capability check up front: whether the harness can hold a **yielded wait on a
process handle** it owns.

- **If it can** (a persistent tool call that blocks on the dispatcher process and returns on
  exit): use exactly the Codex watcher shape above — one real dispatcher process, one yielded
  persistent watcher whose terminal branch emits a notification, one coarse watchdog just beyond
  the bounded-wait window.
- **If it cannot** (no resumable process handle across turns): background the dispatcher, record
  the task ID from stderr, and fall back to the observer contract below — coarse, single-shot
  `ctl status` reads on a bounded cadence sized to the task (a 30-minute review warrants ~5-minute
  checks, not 30-second polls), then one `ctl result` read when status is terminal.

Both variants inherit the hard rules unchanged: never consume the sender inbox from any session
that did not start the dispatch, never stream `ctl watch` into model context, and treat `LLEN`
of an inbox as meaningless for progress. The dispatcher's own exit remains the only completion
signal; polling the durable record is a fallback for harnesses that cannot wait on it, not an
alternative for those that can.

## Observers: read the durable record, never the delivery inbox

A session that did not send the dispatch must never call `BLPOP`, `LPOP`, or
otherwise consume the sender's inbox. The reply is consume-once; a second consumer
can steal it and leave the real dispatcher waiting until timeout.

Use the durable task snapshots instead:

```sh
PYTHONPATH=<bridge-clone>/src python3 -m agent_redis_bridge.ctl \
  --env-file <bridge-env-file> status <task-id>

PYTHONPATH=<bridge-clone>/src python3 -m agent_redis_bridge.ctl \
  --env-file <bridge-env-file> result <task-id>
```

Raw status is a single `HGETALL` of
`<prefix>task:<task-id>:status`; the final payload is stored under the matching
`:result` key. These records are multi-reader and survive consumption of the
inbox reply.

Prefer coarse, single-shot `status`/`result` reads only when an update is needed.
Do not use `ctl watch` or continuously read the task event/transcript stream: it
loads an unbounded activity log into model context. `LLEN` of an agent inbox is
also not progress evidence because a healthy bridge consumes requests immediately.

## Verification boundary

The reply is a claim. For write work, verify the returned commit, worktree status,
diff, and required tests independently before integration. For audited panels,
the reconciled audit close remains the verdict authority.

The bridge also fingerprints the base checkout for worktree isolation. Do not
commit unrelated orchestrator documentation or otherwise advance the base HEAD
while a worktree dispatch is running: even a legitimate orchestrator commit can
make completion fail with `worktree_escape: <base HEAD changed>`. If this occurs,
independently prove the worker worktree commit and root diff before integration;
do not silently relabel the failed completion gate as green.

## Checklist

1. Start one real dispatcher process and retain its process/session handle.
2. Record the task ID printed on stderr.
3. Confirm the durable status reaches `running` if routing is in doubt.
4. Arm one yielded persistent watcher whose terminal branch calls `notify()`, plus
   one coarse watchdog just beyond the bounded-wait window; do not repeatedly
   foreground-wait or consume the sender inbox.
5. Read the captured reply, then verify git/test/audit evidence independently.
